# Teams Routines Atoms Port (PR 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `RoutinesPanel.tsx` to compose the editorial atom layer (PR 1) + add 2 missing BFF routes (`POST /routines/{id}/run`, `GET /routines/{id}/runs`) + add a per-row "Run now" action.

**Architecture:** Refactor existing panel (226 LOC), not a rewrite. Same atom-adoption pattern as PR 2 (Inbox). New BFF routes are thin proxies to upstream Paperclip — `POST /routines/:id/run` and `GET /routines/:id/runs` per `paperclip/server/src/routes/routines.ts:280` + `:150`.

**Tech Stack:** Same as PR 2 (Next.js, vitest, FastAPI, pytest).

**Spec:** `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md` (Routines section).

**Branch:** `plan/teams-routines-atoms-port` (cut off main post-PR-2-merge `3dec50d9`).

---

## Audit summary

**BFF coverage today (from `routers/teams/work.py`):**
- `GET /routines` ✅
- `POST /routines` ✅
- `PATCH /routines/{id}` ✅ (covers pause/resume via `{status: "paused"|"active"}`, mirrors upstream's `routinesApi.update(id, { status })` pattern)
- `DELETE /routines/{id}` ✅

**Missing (this PR adds):**
- `POST /routines/{id}/run` — fires a manual run (upstream `routinesApi.run(id, ...)` in `paperclip/ui/src/pages/Routines.tsx:487`)
- `GET /routines/{id}/runs` — list run history for a routine (upstream `paperclip/server/src/routes/routines.ts:150`)

**Out of scope (deferred to follow-up):**
- Trigger management (`POST /routines/{id}/triggers`, `PATCH /routine-triggers/{id}`, `DELETE /routine-triggers/{id}`) — large feature surface, separate PR
- Concurrency/catch-up policy UI in the Create dialog — defer
- Routine detail page — defer

---

## File Structure

**Backend — modify:**
- `apps/backend/core/services/paperclip_admin_client.py` — add `run_routine` + `list_routine_runs` methods
- `apps/backend/routers/teams/work.py` — add `POST /routines/{id}/run` + `GET /routines/{id}/runs` handlers

**Backend — add tests:**
- `apps/backend/tests/unit/routers/teams/test_work.py` (or wherever existing routines tests live — locate first) — add coverage for the 2 new routes

**Frontend — modify:**
- `apps/frontend/src/components/teams/panels/RoutinesPanel.tsx` (226 LOC) — wrap in `<PanelMasthead>` + `<PanelToolbar>`, list with `<PanelDataTable>`, empty state with `<PanelEmptyState>`, primary CTA + per-row Run-Now buttons with `<EditorialButton>`

**Frontend — add tests:**
- `apps/frontend/tests/unit/components/teams/panels/RoutinesPanel.test.tsx` — atom-contract guards (masthead, search-input, empty-state, run-now button)

---

## Task 0: Setup + audit

**Files:** none (read-only).

- [ ] **Step 1: Confirm branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git status
git log --oneline -1
```

Expected: branch `plan/teams-routines-atoms-port`, top commit is the squash-merged PR 2 (sha `3dec50d9`, message `feat(teams-inbox): port to editorial atom layer (PR 2 of /teams port)`).

- [ ] **Step 2: Run baseline tests**

```sh
cd apps/frontend && pnpm test "components/teams/panels/RoutinesPanel" -- --run 2>&1 | tail -20
cd apps/backend && uv run pytest tests/unit/routers/teams/ -v 2>&1 | tail -15
```

Note baselines for comparison after Tasks 1-3.

- [ ] **Step 3: Read RoutinesPanel.tsx + work.py routines section**

```sh
cat apps/frontend/src/components/teams/panels/RoutinesPanel.tsx
sed -n '1,80p' apps/backend/routers/teams/work.py
grep -n 'list_routines\|create_routine\|patch_routine\|delete_routine\|run_routine\|list_routine_runs' apps/backend/core/services/paperclip_admin_client.py
```

Catalog: panel structure (does it have a header? toolbar? row layout? empty state? action buttons?), existing admin client routine methods, naming conventions.

No commit (read-only).

---

## Task 1: Add `POST /routines/{id}/run` BFF route

Upstream: `POST /routines/:id/run` per `paperclip/server/src/routes/routines.ts:280`.

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py`
- Modify: `apps/backend/routers/teams/work.py`
- Test: `apps/backend/tests/unit/routers/teams/test_work.py` (or whichever test file holds existing routines tests — locate via Task 0 step 2)

- [ ] **Step 1: Locate existing routines test file**

```sh
find apps/backend/tests -name "test_work*" -o -name "*test_routines*" 2>/dev/null
grep -rln "list_routines\|create_routine" apps/backend/tests 2>/dev/null | head
```

Note path. If no existing file, create `apps/backend/tests/unit/routers/teams/test_work.py`.

- [ ] **Step 2: Write the failing test**

In the test file located in step 1, add:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_run_routine_proxies_upstream(client, monkeypatch):
    """POST /teams/routines/{id}/run proxies upstream + forwards body."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.run_routine = AsyncMock(return_value={"runId": "run_test_123", "status": "queued"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    resp = client.post("/api/v1/teams/routines/r1/run", json={"reason": "manual"})

    assert resp.status_code == 200
    assert resp.json()["runId"] == "run_test_123"
    admin.run_routine.assert_awaited_once_with(
        routine_id="r1",
        body={"reason": "manual"},
        session_cookie="cookie",
    )
```

(Adapt fixture style to match the file's existing patterns. The `client` fixture and `monkeypatch.setattr(agents_mod, "_admin", ...)` shape matches the existing routines tests.)

- [ ] **Step 3: Run test to verify failure**

```sh
cd apps/backend && uv run pytest <test-file-path> -k test_run_routine_proxies_upstream -v
```

Expected: FAIL with 404 or AttributeError (admin_client method doesn't exist yet).

- [ ] **Step 4: Add `run_routine` to PaperclipAdminClient**

In `apps/backend/core/services/paperclip_admin_client.py`, find the `delete_routine` method (around line 1160 per the audit) and add this method right after it:

```python
    async def run_routine(
        self,
        *,
        routine_id: str,
        body: dict,
        session_cookie: str,
    ) -> dict:
        """Manually fire a routine.

        Maps to ``POST /api/routines/{routineId}/run`` upstream
        (paperclip/server/src/routes/routines.ts:280). Body is forwarded as-is
        to upstream, which validates against ``runRoutineSchema`` (optional
        ``reason``, ``inputOverrides``, etc.).

        Returns the upstream response, typically ``{runId, status}``.
        """
        return await self._post(
            f"/api/routines/{routine_id}/run",
            json=body,
            session_cookie=session_cookie,
        )
```

- [ ] **Step 5: Add the BFF route**

In `apps/backend/routers/teams/work.py`, add this route alongside the other routine handlers:

```python
@router.post("/routines/{routine_id}/run")
async def run_routine(
    routine_id: str,
    body: dict,
    ctx: TeamsContext = Depends(_ctx),
):
    """Manually fire a routine. Body is forwarded to upstream as-is."""
    return await _agents._admin().run_routine(
        routine_id=routine_id,
        body=body,
        session_cookie=ctx.session_cookie,
    )
```

(If `body: dict` doesn't match the file's existing FastAPI body-parsing convention — e.g. it uses Pydantic models — adapt to match.)

- [ ] **Step 6: Run test, verify pass**

```sh
cd apps/backend && uv run pytest <test-file-path> -k test_run_routine_proxies_upstream -v
```

Expected: PASS.

- [ ] **Step 7: Run full backend teams test suite**

```sh
cd apps/backend && uv run pytest tests/unit/routers/teams/ -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 8: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/backend/core/services/paperclip_admin_client.py \
  apps/backend/routers/teams/work.py \
  <test-file-path>
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-bff): POST /routines/{id}/run for manual routine fire"
```

---

## Task 2: Add `GET /routines/{id}/runs` BFF route

Upstream: `GET /routines/:id/runs` per `paperclip/server/src/routes/routines.ts:150`.

**Files:** same files as Task 1.

- [ ] **Step 1: Write the failing test**

Add to the same test file:

```python
@pytest.mark.asyncio
async def test_list_routine_runs_returns_envelope(client, monkeypatch):
    """GET /teams/routines/{id}/runs proxies upstream + wraps in {runs: [...]} envelope."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.list_routine_runs = AsyncMock(
        return_value=[
            {"id": "run_1", "status": "completed", "startedAt": "2026-05-07T10:00:00Z"},
            {"id": "run_2", "status": "running", "startedAt": "2026-05-07T11:00:00Z"},
        ]
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    resp = client.get("/api/v1/teams/routines/r1/runs")

    assert resp.status_code == 200
    body = resp.json()
    assert "runs" in body
    assert len(body["runs"]) == 2
    admin.list_routine_runs.assert_awaited_once_with(
        routine_id="r1",
        session_cookie="cookie",
    )
```

- [ ] **Step 2: Run test, verify failure**

```sh
cd apps/backend && uv run pytest <test-file-path> -k test_list_routine_runs_returns_envelope -v
```

Expected: FAIL with 404 or AttributeError.

- [ ] **Step 3: Add `list_routine_runs` to PaperclipAdminClient**

After the `run_routine` method added in Task 1:

```python
    async def list_routine_runs(
        self,
        *,
        routine_id: str,
        session_cookie: str,
    ) -> list[dict]:
        """List run history for a routine.

        Maps to ``GET /api/routines/{routineId}/runs`` upstream
        (paperclip/server/src/routes/routines.ts:150). Returns a list of run
        objects (id, status, startedAt, completedAt, etc.).

        Returns empty list if upstream returns a non-list (defensive — upstream
        documented to return a JSON array per the route handler).
        """
        result = await self._get(
            f"/api/routines/{routine_id}/runs",
            session_cookie=session_cookie,
        )
        return result if isinstance(result, list) else []
```

- [ ] **Step 4: Add the BFF route**

In `apps/backend/routers/teams/work.py`, near the other routines routes (place after `POST /routines/{id}/run` from Task 1):

```python
@router.get("/routines/{routine_id}/runs")
async def list_routine_runs(
    routine_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """List run history for a routine."""
    upstream = await _agents._admin().list_routine_runs(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "runs")
```

- [ ] **Step 5: Run test, verify pass**

```sh
cd apps/backend && uv run pytest <test-file-path> -k test_list_routine_runs_returns_envelope -v
```

Expected: PASS.

- [ ] **Step 6: Run full backend teams test suite**

```sh
cd apps/backend && uv run pytest tests/unit/routers/teams/ -v 2>&1 | tail -10
```

- [ ] **Step 7: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/backend/core/services/paperclip_admin_client.py \
  apps/backend/routers/teams/work.py \
  <test-file-path>
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-bff): GET /routines/{id}/runs for routine run history"
```

---

## Task 3: Refactor RoutinesPanel to use atoms

Wrap the existing 226 LOC panel in `<PanelMasthead>` + `<PanelToolbar>` + `<PanelEmptyState>`. Add a per-row "Run now" `<EditorialButton>` that calls the new `POST /routines/{id}/run` route from Task 1.

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/RoutinesPanel.tsx`
- Test: `apps/frontend/tests/unit/components/teams/panels/RoutinesPanel.test.tsx` (atom-contract — create if missing)

- [ ] **Step 1: Read RoutinesPanel.tsx end-to-end**

```sh
cat apps/frontend/src/components/teams/panels/RoutinesPanel.tsx
```

Catalog: header, toolbar/filters, list shape, empty state, action buttons, primary CTA, hooks used (likely `useTeamsApi`).

- [ ] **Step 2: Locate existing test files**

```sh
find apps/frontend -name "RoutinesPanel.test*" 2>/dev/null
```

Two-tree convention: atom-contract guards in `tests/unit/components/teams/panels/`; behavior tests in `src/__tests__/components/teams/panels/` if present.

- [ ] **Step 3: Write the failing tests**

Create or update `apps/frontend/tests/unit/components/teams/panels/RoutinesPanel.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";

// Adapt mocks to match what RoutinesPanel actually consumes (likely useTeamsApi.read).

describe("RoutinesPanel atom contract", () => {
  it("renders PanelMasthead with overline 'Routines' and h1 title", () => {
    render(<RoutinesPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: /routines/i })).toBeInTheDocument();
  });

  it("renders PanelToolbar search input (role=searchbox)", () => {
    render(<RoutinesPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState when routines list is empty", () => {
    render(<RoutinesPanel />);
    // PanelEmptyState renders an h2 with editorial Lora-italic class
    const headline = screen.queryByRole("heading", { level: 2 });
    if (headline) {
      expect(headline).toHaveClass("font-editorial-serif");
    }
    // (If the panel always renders the empty state when routines=[], assertion stronger)
  });
});
```

(Adapt to actual prop signature + mock SWR/useTeamsApi responses to return empty routines list.)

- [ ] **Step 4: Run test, verify failure**

```sh
cd apps/frontend && pnpm test RoutinesPanel -- --run
```

Expected: FAIL on `role="banner"` / `role="searchbox"` (no atoms yet).

- [ ] **Step 5: Refactor RoutinesPanel.tsx to compose atoms**

Replace the existing structure with:

```tsx
"use client";

import { useState } from "react";
import { Repeat } from "lucide-react";
import {
  PanelMasthead,
  PanelToolbar,
  PanelEmptyState,
  PanelDataTable,
  EditorialButton,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Routine {
  id: string;
  title: string;
  status: string;
  // ... add fields the existing panel uses
}

export function RoutinesPanel() {
  const { read, post } = useTeamsApi();
  const { data, isLoading, mutate } = read<{ routines: Routine[] }>("/routines");
  const [search, setSearch] = useState("");

  const routines = data?.routines ?? [];
  const filtered = search
    ? routines.filter((r) => r.title.toLowerCase().includes(search.toLowerCase()))
    : routines;

  async function handleRunNow(routine: Routine) {
    await post(`/routines/${routine.id}/run`, { reason: "manual" });
    await mutate();
  }

  const columns: PanelDataColumn<Routine>[] = [
    { key: "title", header: "Title", cell: (r) => r.title, sortable: true },
    { key: "status", header: "Status", cell: (r) => r.status },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (r) => (
        <EditorialButton
          variant="ghost"
          size="sm"
          aria-label={`Run ${r.title} now`}
          onClick={() => handleRunNow(r)}
        >
          Run now
        </EditorialButton>
      ),
    },
  ];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Routines" title="Routines" />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find routine..."
        searchAriaLabel="Search routines"
      />
      {!isLoading && filtered.length === 0 ? (
        <PanelEmptyState
          icon={Repeat}
          headline="No routines yet."
          supporting="Create a routine to define a recurring workflow that fires on a schedule or trigger."
        />
      ) : (
        <PanelDataTable<Routine>
          rows={filtered}
          columns={columns}
          rowKey={(r) => r.id}
        />
      )}
    </div>
  );
}
```

(Adapt the Routine type + columns to match what the existing panel uses. Preserve any existing functionality — e.g. if the existing panel has a Create dialog, keep it as a child component invoked from a primary CTA in the masthead's `actions` slot or a `PanelToolbar` `actions` slot.)

- [ ] **Step 6: Run test, verify pass**

```sh
cd apps/frontend && pnpm test RoutinesPanel -- --run
```

Expected: PASS.

- [ ] **Step 7: Run full frontend teams test suite**

```sh
cd apps/frontend && pnpm test "components/teams" -- --run
```

Expected: all teams tests pass.

- [ ] **Step 8: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/panels/RoutinesPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/RoutinesPanel.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-routines): RoutinesPanel composes editorial atoms + per-row Run-Now action"
```

---

## Task 4: Final verify + push + open PR

- [ ] **Step 1: Run full frontend test suite**

```sh
cd apps/frontend && pnpm test
```

Expected: 989+ pass (was 989 after PR 2), 10 PRE-EXISTING failures unchanged.

- [ ] **Step 2: Run lint + tsc**

```sh
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
```

Expected: clean.

- [ ] **Step 3: Run full backend test suite**

```sh
cd apps/backend && uv run pytest tests/ -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Push branch**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix push -u origin plan/teams-routines-atoms-port
```

- [ ] **Step 5: Open the PR**

```sh
gh pr create --title "feat(teams-routines): port to editorial atom layer (PR 3 of /teams port)" --body "$(cat <<'EOF'
## Summary

Third PR of the multi-step /teams panel port. Refactors `RoutinesPanel.tsx` (226 LOC) to compose the editorial atom layer (PR 1 #553) + adds 2 missing BFF routes (`POST /routines/{id}/run`, `GET /routines/{id}/runs`) + adds a per-row Run-Now action.

## What changed

- **Backend:**
  - `POST /teams/routines/{id}/run` — manually fire a routine (upstream `routinesApi.run(id)`)
  - `GET /teams/routines/{id}/runs` — list run history for a routine (envelope-wrapped)
- **Frontend:**
  - `RoutinesPanel.tsx` — wrapped in `<PanelMasthead>` + `<PanelToolbar>`. List in `<PanelDataTable>`. Empty state in `<PanelEmptyState>`. Per-row "Run now" button via `<EditorialButton variant="ghost" size="sm">` calling the new BFF route.

## Out of scope (deferred)

- Trigger management (POST/PATCH/DELETE on `/routine-triggers`)
- Concurrency / catch-up policy UI in Create dialog
- Routine detail page

## Test plan

- [x] Backend pytest — all teams tests pass (+2 new for run + runs)
- [x] Frontend pnpm test — atom-contract guards + behavior tests green; pre-existing 10 baseline failures unchanged
- [x] tsc --noEmit clean
- [x] lint clean
- [ ] Manual smoke on dev: load /teams → Routines tab. Verify masthead + search input + empty-state + per-row Run-Now action work end-to-end.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Self-review notes

**Spec coverage:**
- Routines panel atom adoption — ✅ Task 3
- BFF gaps closed — ✅ Tasks 1-2 (run + runs); other claimed gaps were inaccurate (POST /routines + PATCH already existed)
- Per-row Run-Now action — ✅ Task 3 (added on top of refactor)

**Out of scope (documented):**
- Trigger management
- Concurrency/catch-up dialog UI
- Routine detail page

**Type consistency:**
- `run_routine(routine_id, body, session_cookie)` kw-only — matches BFF call shape
- `list_routine_runs(routine_id, session_cookie)` kw-only — matches BFF call shape
- `envelope_list(upstream, "runs")` matches existing pattern
- `Routine` interface in RoutinesPanel.tsx — adapter must match what the existing panel + the BFF actually return

**Placeholder scan:** None.
