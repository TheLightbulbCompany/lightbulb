# Teams Run-Detail View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make agent runs inspectable and operable from the Teams surface: a real run-detail panel (metadata, transcript, events timeline), reachable from every run row, with retry/cancel actions and watchdog decisions.

**Architecture:** Finish the surface that was deferred at roadmap row "#3d.2": rebuild the orphaned `RunDetailPanel` stub on the editorial detail-page template, wire it into the dockview panel grammar (`?panel=teams-run-detail&detailId=<runId>`), and link every heartbeat-run row into it. The BFF already passes through single-run + log; add three thin pass-throughs (events, cancel, watchdog-decisions) and a `payload` field on the wake body. All upstream contracts below were verified against the deployed fork source (`~/Desktop/paperclip-fork`), not the vanilla clone.

**Tech Stack:** FastAPI BFF (`apps/backend/routers/teams/`), `PaperclipAdminClient` (httpx, per-user session cookie), Next.js 16 + SWR + Tailwind v4 frontend, vitest + pytest.

## Global Constraints

- **Never edit `isol8.nosync/paperclip/`** — that clone is vanilla upstream, reference only. The deployed truth is the fork at `~/Desktop/paperclip-fork` (do not run git commands there; grep/read files directly).
- **Upstream `status` is a free string.** Verified vocab (fork `packages/shared/src/constants.ts:644`): `queued | scheduled_retry | running | succeeded | failed | cancelled | timed_out` — but render defensively; never `switch` exhaustively.
- **Teams BFF bodies:** all schemas extend `_Strict` (`extra="forbid"`, `alias_generator=to_camel`, `populate_by_name=True`); every router forwarding a body upstream must call `model_dump(exclude_none=True, by_alias=True)` (see `routers/teams/schemas.py:19-40`).
- **No PR/bug references in code comments.**
- **Frontend unit tests:** run ONE file at a time (`pnpm vitest run <file>`) — the suite is RAM-heavy. Frontend CI runs `tsc` over test files too, so test code must typecheck.
- **Backend tests:** `cd apps/backend && uv run pytest <file> -v`.
- **Commit per task; do not push or open a PR without explicit approval.**
- Copy tokens/styling from neighbors: `var(--ink|--mute|--danger|--forest|--warn|--edge|--cream)`, `section-label` headers, `editorial-page` grid (`RoutineDetailPanel.tsx` is the canonical detail-page template).
- **Do NOT link routine run-history rows to the run-detail panel.** `RoutineRun.id` is a `routine_runs` row, not a `heartbeat_runs` id — the detail routes 404 on it. Only heartbeat-run ids link: `IssueRun.runId`, `HeartbeatRun.id`, `LiveRun.id` (all are `heartbeat_runs.id`).

## Verified upstream contracts (fork source, read 2026-07-12)

| Contract | Where verified |
|---|---|
| `GET /api/heartbeat-runs/{id}` → full run row + injected `retryExhaustedReason` + `outputSilence` | `server/src/routes/agents.ts:3455` |
| `GET /api/heartbeat-runs/{id}/events?afterSeq=&limit=` → **raw JSON array** of events (redacted), afterSeq default 0, limit default 200 (service clamps 1–1000) | `server/src/routes/agents.ts:3532` |
| Event shape: `{id, companyId, runId, agentId, seq, eventType, stream: "system"\|"stdout"\|"stderr"\|null, level: "info"\|"warn"\|"error"\|null, color, message, payload, createdAt}` | `packages/shared/src/types/heartbeat.ts:95` |
| `POST /api/heartbeat-runs/{id}/cancel` → board-actor only + company access; returns the cancelled run row (JSON `null` if the service returns nothing) | `server/src/routes/agents.ts:3472` |
| `POST /api/heartbeat-runs/{id}/watchdog-decisions` body: `decision ∈ {snooze, continue, dismissed_false_positive}` (else 400), `evaluationIssueId?`, `reason?` (sliced to 4000), `snoozedUntil` **required future ISO when decision="snooze"** (else 400); returns the inserted decision row | `server/src/routes/agents.ts:3496-3524` |
| `POST /api/agents/{id}/wakeup` body (`wakeAgentSchema`): `source` enum (default `on_demand`), `triggerDetail` enum, `reason?`, **`payload: record(string, unknown)?`**, `idempotencyKey?`, `forceFreshSession?`; responds **202** with the new run (or a "skipped" envelope) | `packages/shared/src/validators/agent.ts:129-139`, `server/src/routes/agents.ts:3173-3243` |
| Retry convention: wake with `reason: "retry_failed_run"`, `payload: {issueId?, taskId?, taskKey?}` (from the run's `contextSnapshot`); shown when status is `failed` or `timed_out`. Resume: `reason: "resume_process_lost_run"`, `payload: {resumeFromRunId, ...}`; shown when `errorCode === "process_lost" && status === "failed"` | `ui/src/pages/AgentDetail.tsx:3137-3190` |
| Usage line derivation: `usageJson` keys `inputTokens|input_tokens`, `outputTokens|output_tokens`, `cachedInputTokens|cached_input_tokens|cache_read_input_tokens`, `provider`, `model` | `ui/src/pages/AgentDetail.tsx:277-301` |
| Log envelope `{runId, store, logRef, content, nextOffset?}`; content is NDJSON `{ts, stream, chunk}` lines; run row carries `logBytes` for tail-offset math | `server/src/routes/agents.ts:3554`, `AgentDetail.tsx:303` |

---

## Phase 1 — Make runs inspectable

### Task 1: BFF run-events pass-through

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (insert after `get_run_log`, ~line 996)
- Modify: `apps/backend/routers/teams/runs.py`
- Test: `apps/backend/tests/unit/routers/teams/test_runs.py`

**Interfaces:**
- Consumes: existing `_agents._admin()` / `_ctx` plumbing in `runs.py`, `envelope_list` from `routers/teams/deps.py`.
- Produces: `GET /api/v1/teams/heartbeat-runs/{run_id}/events?afterSeq=&limit=` → `{"events": [...]}`; client method `get_run_events(*, session_cookie, run_id, after_seq=0, limit=200) -> list | dict`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/routers/teams/test_runs.py`)

```python
def test_get_run_events_defaults(client, monkeypatch):
    """`GET /teams/heartbeat-runs/{id}/events` wraps upstream's raw event
    array in the standard list envelope, with afterSeq=0 / limit=200."""
    admin = MagicMock()
    admin.get_run_events = AsyncMock(
        return_value=[
            {
                "id": 1,
                "runId": "run_1",
                "seq": 1,
                "eventType": "lifecycle",
                "level": "info",
                "message": "run started",
            }
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/events",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["events"][0]["eventType"] == "lifecycle"
    admin.get_run_events.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        after_seq=0,
        limit=200,
    )


def test_get_run_events_passes_after_seq_and_limit(client, monkeypatch):
    """The route forwards `afterSeq` and `limit` and clamps limit to 1000."""
    admin = MagicMock()
    admin.get_run_events = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/events?afterSeq=17&limit=50",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_run_events.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        after_seq=17,
        limit=50,
    )

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/events?limit=99999",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_runs.py -v`
Expected: the two new tests FAIL (404 — route not defined); the three existing tests PASS.

- [ ] **Step 3: Add the client method** (in `paperclip_admin_client.py`, directly after `get_run_log`)

```python
    async def get_run_events(
        self,
        *,
        session_cookie: str,
        run_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list | dict:
        """List a heartbeat run's structured event timeline.

        Maps to ``GET /api/heartbeat-runs/{runId}/events``. Upstream returns
        a raw JSON array of ``heartbeat_run_events`` rows (seq-ordered,
        payloads redacted server-side); ``afterSeq`` pages forward and
        ``limit`` is clamped upstream to 1..1000.
        """
        return await self._get(
            f"/api/heartbeat-runs/{run_id}/events",
            session_cookie=session_cookie,
            params={"afterSeq": str(after_seq), "limit": str(limit)},
        )
```

- [ ] **Step 4: Add the route** (append to `routers/teams/runs.py`; extend the imports at the top of the file)

```python
from .deps import TeamsContext, envelope_list
```

```python
@router.get("/heartbeat-runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0, alias="afterSeq"),
    limit: int = Query(200, ge=1, le=1000),
    ctx: TeamsContext = Depends(_ctx),
):
    """List the structured event timeline for a heartbeat run.

    Maps to upstream ``GET /api/heartbeat-runs/{runId}/events``. Upstream
    responds with a raw array; wrap it in the standard list envelope so the
    frontend reads ``data.events`` like every other Teams list surface.
    """
    rows = await _agents._admin().get_run_events(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
        after_seq=after_seq,
        limit=limit,
    )
    return envelope_list(rows, "events")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_runs.py -v`
Expected: all 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/paperclip_admin_client.py apps/backend/routers/teams/runs.py apps/backend/tests/unit/routers/teams/test_runs.py
git commit -m "feat(teams): heartbeat-run events pass-through for the run-detail view"
```

---

### Task 2: Frontend run types + full-tail transcript hook

**Files:**
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (after `IssueRun`, ~line 340)
- Modify: `apps/frontend/src/components/teams/dashboard/hooks/useRunLog.ts` (export `parseLogLines`)
- Create: `apps/frontend/src/components/teams/runs/useRunTranscript.ts`
- Test: `apps/frontend/src/__tests__/teams/useRunTranscript.test.ts`

**Interfaces:**
- Consumes: `useTeamsApi().read`, `parseLogLines(content: string): string[]` (newly exported from `useRunLog.ts`).
- Produces:
  - `interface RunDetail` and `interface RunEvent` in `shared/types.ts` (exact shapes below) — Task 3 renders these.
  - `useRunTranscript(runId: string, opts: { active: boolean; logBytes?: number | null }): { lines: string[]; truncated: boolean; isLoading: boolean }` — Task 3 consumes this.

- [ ] **Step 1: Add the types** (in `shared/types.ts`, directly after `IssueRun`)

```ts
/** Full heartbeat-run row for the run-detail panel (upstream
 *  `GET /api/heartbeat-runs/{id}`, verbatim pass-through via the BFF).
 *  Field set verified against the fork's shared HeartbeatRun type; every
 *  field is optional-defensive because upstream row shapes drift. */
export interface RunDetail {
  id: string;
  agentId?: string | null;
  /** Free string; known vocab: queued | scheduled_retry | running |
   *  succeeded | failed | cancelled | timed_out. Render defensively. */
  status?: string | null;
  invocationSource?: string | null;
  triggerDetail?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
  error?: string | null;
  errorCode?: string | null;
  exitCode?: number | null;
  signal?: string | null;
  usageJson?: Record<string, unknown> | null;
  sessionIdBefore?: string | null;
  sessionIdAfter?: string | null;
  logBytes?: number | null;
  stdoutExcerpt?: string | null;
  stderrExcerpt?: string | null;
  retryOfRunId?: string | null;
  retryExhaustedReason?: string | null;
  livenessState?: string | null;
  livenessReason?: string | null;
  nextAction?: string | null;
  lastUsefulActionAt?: string | null;
  contextSnapshot?: Record<string, unknown> | null;
  outputSilence?: RunOutputSilence | null;
}

/** One `heartbeat_run_events` row (upstream events timeline). */
export interface RunEvent {
  id?: number;
  seq: number;
  eventType?: string | null;
  stream?: string | null;
  level?: string | null;
  message?: string | null;
  payload?: Record<string, unknown> | null;
  createdAt?: string | null;
}
```

Also add the import at the top of `types.ts` if not already present:

```ts
import type { RunOutputSilence } from "@/components/teams/shared/lib/liveRuns";
```

(Verified cycle-safe: `liveRuns.ts` imports nothing from `types.ts`, so this type-only import cannot create a circular dependency.)

- [ ] **Step 2: Export the parser** (in `useRunLog.ts`, change the `function parseLogLines` declaration only)

```ts
export function parseLogLines(content: string): string[] {
```

- [ ] **Step 3: Write the failing test** (`src/__tests__/teams/useRunTranscript.test.ts`)

```ts
// apps/frontend/src/__tests__/teams/useRunTranscript.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { useRunTranscript, RUN_TRANSCRIPT_LIMIT_BYTES } from "@/components/teams/runs/useRunTranscript";

beforeEach(() => read.mockReset());

const line = (chunk: string) => JSON.stringify({ ts: "t", stream: "stdout", chunk }) + "\n";

describe("useRunTranscript", () => {
  it("reads from offset 0 for small logs and reports untruncated", () => {
    read.mockReturnValue({
      data: { content: line("hello") + line("world") },
      isLoading: false,
    });
    const { result } = renderHook(() =>
      useRunTranscript("r1", { active: false, logBytes: 128 }),
    );
    expect(read).toHaveBeenCalledWith(
      `/heartbeat-runs/r1/log?limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`,
      expect.anything(),
    );
    expect(result.current.lines).toEqual(["hello", "world"]);
    expect(result.current.truncated).toBe(false);
  });

  it("tail-reads oversized logs from logBytes-cap and reports truncated", () => {
    read.mockReturnValue({ data: { content: line("tail") }, isLoading: false });
    const logBytes = RUN_TRANSCRIPT_LIMIT_BYTES + 5_000;
    const { result } = renderHook(() =>
      useRunTranscript("r1", { active: false, logBytes }),
    );
    expect(read).toHaveBeenCalledWith(
      `/heartbeat-runs/r1/log?offset=5000&limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`,
      expect.anything(),
    );
    expect(result.current.truncated).toBe(true);
  });
});
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/useRunTranscript.test.ts`
Expected: FAIL — module `useRunTranscript` not found.

- [ ] **Step 5: Implement the hook** (`src/components/teams/runs/useRunTranscript.ts`)

```ts
// apps/frontend/src/components/teams/runs/useRunTranscript.ts
//
// Full-scrollback transcript read for the run-detail panel.
//
// Unlike the dashboard-card hook (useRunLog: bounded tail, last 12 lines),
// the detail panel wants the whole log — bounded by the BFF's 256 KiB
// per-read ceiling. Rather than walking nextOffset with sequential reads,
// do ONE bounded read aimed at the tail: the run row carries `logBytes`, so
// when the log exceeds the cap we start the read at `logBytes - cap` and
// surface a truncation flag for the panel to render a notice.
"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { parseLogLines } from "@/components/teams/dashboard/hooks/useRunLog";

/** Matches the BFF's RUN_LOG_MAX_LIMIT_BYTES ceiling. */
export const RUN_TRANSCRIPT_LIMIT_BYTES = 262_144;

/** Poll cadence while the run is active — same beat as the live-runs feed. */
const REFRESH_MS = 15_000;

interface RunLogResponse {
  content?: string | null;
}

export function useRunTranscript(
  runId: string,
  opts: { active: boolean; logBytes?: number | null },
): { lines: string[]; truncated: boolean; isLoading: boolean } {
  const { read } = useTeamsApi();
  const truncated =
    typeof opts.logBytes === "number" && opts.logBytes > RUN_TRANSCRIPT_LIMIT_BYTES;
  const offset = truncated
    ? (opts.logBytes as number) - RUN_TRANSCRIPT_LIMIT_BYTES
    : 0;
  const path = truncated
    ? `/heartbeat-runs/${runId}/log?offset=${offset}&limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`
    : `/heartbeat-runs/${runId}/log?limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`;

  const { data, isLoading } = read<RunLogResponse>(path, {
    refreshInterval: opts.active ? REFRESH_MS : 0,
    dedupingInterval: 2000,
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });

  const lines = useMemo(() => {
    const content = data?.content;
    if (!content) return [];
    return parseLogLines(content);
  }, [data?.content]);

  return { lines, truncated, isLoading };
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/useRunTranscript.test.ts`
Expected: PASS. Also confirm the exporting change didn't break the card hook:
Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/DashboardPanel.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/teams/shared/types.ts apps/frontend/src/components/teams/dashboard/hooks/useRunLog.ts apps/frontend/src/components/teams/runs/useRunTranscript.ts apps/frontend/src/__tests__/teams/useRunTranscript.test.ts
git commit -m "feat(teams): run-detail types + full-tail transcript hook"
```

---

### Task 3: Rebuild RunDetailPanel

**Files:**
- Rewrite: `apps/frontend/src/components/teams/panels/RunDetailPanel.tsx`
- Rewrite test: `apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx`

**Interfaces:**
- Consumes: `RunDetail`, `RunEvent`, `CompanyAgent` from `shared/types`; `useRunTranscript` (Task 2); BFF reads `/heartbeat-runs/{id}`, `/heartbeat-runs/{id}/events`, `/agents`.
- Produces: `export function RunDetailPanel({ runId }: { runId: string })` — Task 4 mounts it under panel key `run-detail`. Keep the legacy `/runs/{id}` fallback read (some older rows are generic runs).

- [ ] **Step 1: Rewrite the test file** (`RunDetailPanel.test.tsx`)

```tsx
// apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
const post = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read, post }) }));

import { RunDetailPanel } from "@/components/teams/panels/RunDetailPanel";

const swrIdle = { data: undefined, isLoading: false, error: null, mutate: vi.fn() };

function mockReads(overrides: Record<string, unknown>) {
  read.mockImplementation((path: string) => {
    for (const [prefix, data] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) {
        return { data, isLoading: false, error: null, mutate: vi.fn() };
      }
    }
    return swrIdle;
  });
}

beforeEach(() => {
  read.mockReset();
  post.mockReset();
});

describe("RunDetailPanel", () => {
  it("renders status, agent, failure block, usage and transcript", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: '{"ts":"t","stream":"stdout","chunk":"doing work"}\n' },
      "/heartbeat-runs/r1/events": { events: [{ seq: 1, eventType: "lifecycle", level: "info", message: "run started" }] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "timed_out",
        startedAt: "2026-07-09T02:09:40Z",
        finishedAt: "2026-07-09T02:11:47Z",
        error: "gateway wait gave up",
        retryExhaustedReason: "max retries reached",
        usageJson: { inputTokens: 1200, outputTokens: 300, provider: "bedrock" },
        logBytes: 64,
      },
      "/agents": { agents: [{ id: "agt_1", name: "Lens" }] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText("timed out")).toBeInTheDocument();
    expect(screen.getAllByText("Lens").length).toBeGreaterThan(0);
    expect(screen.getByText("gateway wait gave up")).toBeInTheDocument();
    expect(screen.getByText("max retries reached")).toBeInTheDocument();
    expect(screen.getByText("doing work")).toBeInTheDocument();
    expect(screen.getByText("run started")).toBeInTheDocument();
    expect(screen.getByText(/1,200 in/)).toBeInTheDocument();
  });

  it("falls back to the legacy generic run when the heartbeat-run 404s", () => {
    read.mockImplementation((path: string) => {
      if (path.startsWith("/heartbeat-runs/r1") ) {
        return { data: undefined, isLoading: false, error: new Error("404"), mutate: vi.fn() };
      }
      if (path === "/runs/r1") {
        return { data: { id: "r1", status: "succeeded" }, isLoading: false, error: null, mutate: vi.fn() };
      }
      return swrIdle;
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });

  it("shows the truncation notice for oversized logs", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", status: "running", logBytes: 5_000_000 },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText(/showing the last 256 KiB/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/RunDetailPanel.test.tsx`
Expected: FAIL (old stub renders none of this).

- [ ] **Step 3: Rewrite the panel**

```tsx
// apps/frontend/src/components/teams/panels/RunDetailPanel.tsx
"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useRunTranscript } from "@/components/teams/runs/useRunTranscript";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, RunDetail, RunEvent } from "@/components/teams/shared/types";

// Free upstream string — color the known vocabulary, default to mute.
function statusColor(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return "var(--forest)";
  if (s === "failed" || s === "error" || s === "cancelled" || s === "timed_out") return "var(--danger)";
  if (s === "running" || s === "queued" || s === "scheduled_retry") return "var(--forest)";
  return "var(--mute)";
}

function isActiveStatus(status: string | null | undefined): boolean {
  const s = (status ?? "").toLowerCase();
  return s === "running" || s === "queued" || s === "scheduled_retry";
}

// Mirrors upstream's runMetrics: usageJson carries camelCase or snake_case
// token keys depending on provider adapter.
function usageNumber(u: Record<string, unknown> | null | undefined, ...keys: string[]): number | null {
  if (!u) return null;
  for (const k of keys) {
    const v = u[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

function fmtWhen(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? null : `${t.toLocaleString()} (${timeAgo(iso)})`;
}

function fmtDuration(start?: string | null, end?: string | null): string | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function Prop({ label, children }: { label: string; children: React.ReactNode }) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2" style={{ borderBottom: "1px solid var(--edge)" }}>
      <dt className="text-[10px] uppercase tracking-wide" style={{ color: "var(--mute)" }}>
        {label}
      </dt>
      <dd className="text-sm break-words text-[var(--ink)]">{children}</dd>
    </div>
  );
}

export function RunDetailPanel({ runId }: { runId: string }) {
  const { read } = useTeamsApi();

  // A run link can target a heartbeat-run (the normal case) or a legacy
  // generic run; read both, prefer the heartbeat-run.
  const hb = read<RunDetail>(`/heartbeat-runs/${runId}`, { shouldRetryOnError: false });
  const legacy = read<RunDetail>(`/runs/${runId}`, { shouldRetryOnError: false });
  const run = hb.data ?? legacy.data ?? null;
  const isLoading = (hb.isLoading || legacy.isLoading) && !run;

  const active = isActiveStatus(run?.status);
  const { data: eventsData } = read<{ events: RunEvent[] }>(
    `/heartbeat-runs/${runId}/events`,
    {
      refreshInterval: active ? 15_000 : 0,
      revalidateOnFocus: false,
      shouldRetryOnError: false,
    },
  );
  const events = eventsData?.events ?? [];

  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const agentName = useMemo(() => {
    const agents = agentsData?.agents ?? [];
    return agents.find((a) => a.id === run?.agentId)?.name ?? run?.agentId ?? "Agent";
  }, [agentsData?.agents, run?.agentId]);

  const transcript = useRunTranscript(runId, {
    active,
    logBytes: run?.logBytes ?? null,
  });

  if (isLoading) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{ fontFamily: "var(--font-lora-serif), serif", color: "var(--mute)" }}
      >
        Loading…
      </div>
    );
  }

  if (!run) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load run.</p>
      </div>
    );
  }

  const usage = run.usageJson ?? null;
  const input = usageNumber(usage, "inputTokens", "input_tokens");
  const output = usageNumber(usage, "outputTokens", "output_tokens");
  const cached = usageNumber(usage, "cachedInputTokens", "cached_input_tokens", "cache_read_input_tokens");
  const provider = typeof usage?.provider === "string" ? usage.provider : null;
  const model = typeof usage?.model === "string" ? usage.model : null;
  const statusLabel = (run.status ?? "unknown").replace(/_/g, " ");

  return (
    <div
      className="editorial-page grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <main className="flex min-w-0 flex-col gap-6">
        <div className="flex flex-col gap-2">
          <h1 className="font-editorial-serif text-2xl font-medium leading-tight text-[var(--ink)]">
            Run — {agentName}
          </h1>
          <div className="flex items-center gap-2 text-sm">
            <span className="capitalize" style={{ color: statusColor(run.status) }}>
              {statusLabel}
            </span>
            {run.startedAt && (
              <span className="text-xs" style={{ color: "var(--mute)" }}>
                started {timeAgo(run.startedAt)}
              </span>
            )}
          </div>
        </div>

        {(run.error || run.retryExhaustedReason || run.stderrExcerpt) && (
          <section
            className="flex flex-col gap-1 rounded px-3 py-2 text-sm"
            style={{
              border: "1px solid color-mix(in srgb, var(--danger) 35%, var(--edge))",
              background: "color-mix(in srgb, var(--danger) 8%, transparent)",
            }}
          >
            {run.error && <p style={{ color: "var(--danger)" }}>{run.error}</p>}
            {run.retryExhaustedReason && (
              <p style={{ color: "var(--danger)" }}>{run.retryExhaustedReason}</p>
            )}
            {run.stderrExcerpt && (
              <pre className="whitespace-pre-wrap text-xs" style={{ color: "var(--mute)" }}>
                {run.stderrExcerpt}
              </pre>
            )}
          </section>
        )}

        <section className="flex flex-col gap-3">
          <h2 className="section-label"><b>Transcript</b></h2>
          {transcript.truncated && (
            <p className="text-xs italic" style={{ color: "var(--mute)" }}>
              Log is larger than one read — showing the last 256 KiB.
            </p>
          )}
          {transcript.lines.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              {transcript.isLoading ? "Loading transcript…" : "No transcript captured."}
            </p>
          ) : (
            <pre
              className="max-h-[50vh] overflow-auto whitespace-pre-wrap rounded px-3 py-2 font-mono text-xs"
              style={{ border: "1px solid var(--edge)", background: "var(--cream)" }}
            >
              {transcript.lines.join("\n")}
            </pre>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="section-label"><b>Events</b></h2>
          {events.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              No events recorded.
            </p>
          ) : (
            <ul className="flex flex-col">
              {events.map((e) => (
                <li
                  key={e.seq}
                  className="flex items-baseline gap-2 px-3 py-1.5 text-xs"
                  style={{ borderBottom: "1px solid var(--edge)" }}
                >
                  <span
                    className="flex-shrink-0 uppercase tracking-wide"
                    style={{ color: e.level === "error" ? "var(--danger)" : e.level === "warn" ? "var(--warn)" : "var(--mute)" }}
                  >
                    {e.eventType ?? "event"}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[var(--ink)]">{e.message ?? ""}</span>
                  {e.createdAt && (
                    <span className="flex-shrink-0" style={{ color: "var(--mute)" }}>
                      {timeAgo(e.createdAt)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <aside className="lg:sticky lg:top-4 lg:self-start">
        <dl className="flex flex-col">
          <Prop label="Agent">{agentName}</Prop>
          <Prop label="Trigger">
            {[run.invocationSource, run.triggerDetail].filter(Boolean).join(" · ") || null}
          </Prop>
          <Prop label="Started">{fmtWhen(run.startedAt)}</Prop>
          <Prop label="Finished">{fmtWhen(run.finishedAt)}</Prop>
          <Prop label="Duration">{fmtDuration(run.startedAt, run.finishedAt)}</Prop>
          <Prop label="Usage">
            {input !== null || output !== null
              ? `${(input ?? 0).toLocaleString()} in / ${(output ?? 0).toLocaleString()} out${cached ? ` (${cached.toLocaleString()} cached)` : ""}`
              : null}
          </Prop>
          <Prop label="Model">{[provider, model].filter(Boolean).join(" · ") || null}</Prop>
          <Prop label="Error code">{run.errorCode}</Prop>
          <Prop label="Exit">
            {typeof run.exitCode === "number" ? String(run.exitCode) : null}
          </Prop>
          <Prop label="Retry of">{run.retryOfRunId}</Prop>
          <Prop label="Liveness">
            {[run.livenessState, run.livenessReason].filter(Boolean).join(" — ") || null}
          </Prop>
          <Prop label="Next action">{run.nextAction}</Prop>
          <Prop label="Session before">{run.sessionIdBefore}</Prop>
          <Prop label="Session after">{run.sessionIdAfter}</Prop>
          <Prop label="Run id">{run.id}</Prop>
        </dl>
      </aside>
    </div>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/RunDetailPanel.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/teams/panels/RunDetailPanel.tsx apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx
git commit -m "feat(teams): rebuild RunDetailPanel — metadata, failure block, transcript, events"
```

---

### Task 4: Panel wiring — make the run-detail panel reachable

**Files:**
- Modify: `apps/frontend/src/components/workspace/panelRegistry.ts` (`TEAMS_DETAIL_KEYS` ~line 52; `durableParamsFor` regex ~line 183)
- Modify: `apps/frontend/src/components/teams/TeamsPanelRouter.tsx` (`DETAIL_LOADERS` ~line 44; detail branch ~line 107)
- Modify: `apps/frontend/src/lib/shellNav.ts` (`teamsDetailUrl` kind union, line 36)
- Test: `apps/frontend/src/__tests__/teams/TeamsPanelRouter.detail.test.tsx` (extend)

**Interfaces:**
- Consumes: `RunDetailPanel` (Task 3).
- Produces: URL grammar `?panel=teams-run-detail&detailId=<heartbeatRunId>`; `teamsDetailUrl("run", id)` — Task 5's links depend on this.
- **Deliberately NOT changed:** `WorkspaceShell.tsx` `TEAMS_DETAIL_PANEL_ID_RE` / `DETAIL_KEY_BY_LIST_KEY` — those only power the detail→list "leaveToList" swap, and a run detail has no list panel to leave to.

- [ ] **Step 1: Extend the router detail test.** Open `TeamsPanelRouter.detail.test.tsx`, copy the existing `issue-detail` case's mocking pattern exactly (it mocks the dynamic panel imports), and add:

```tsx
it("renders RunDetailPanel for panel=run-detail", async () => {
  render(<TeamsPanelRouter panel="run-detail" detailId="r1" />);
  // The dynamic chunk resolves async; the mocked RunDetailPanel renders a marker.
  expect(await screen.findByTestId("run-detail-panel")).toBeInTheDocument();
});
```

with a mock alongside the file's existing panel mocks:

```tsx
vi.mock("@/components/teams/panels/RunDetailPanel", () => ({
  RunDetailPanel: ({ runId }: { runId: string }) => (
    <div data-testid="run-detail-panel">{runId}</div>
  ),
}));
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/TeamsPanelRouter.detail.test.tsx`
Expected: new case FAILS (router falls through to "Unknown panel").

- [ ] **Step 3: Wire the router** (`TeamsPanelRouter.tsx`)

Add to `DETAIL_LOADERS`:

```ts
  run: () => import("./panels/RunDetailPanel").then((m) => m.RunDetailPanel),
```

Add beside the other dynamic detail wrappers:

```ts
const RunDetailPanel = dynamic(DETAIL_LOADERS.run, { loading: PanelSkeleton });
```

Add to the detail branch (after the `goal-detail` line):

```tsx
    if (panel === "run-detail")
      return <RunDetailPanel runId={detailId} />;
```

Note: `DETAIL_LOADERS.run` is automatically included in `preloadTeamsPanels()` since it iterates `Object.values(DETAIL_LOADERS)`.

- [ ] **Step 4: Wire the registry** (`panelRegistry.ts`)

```ts
const TEAMS_DETAIL_KEYS: Readonly<Record<string, string>> = {
  "issue-detail": "Task",
  "routine-detail": "Routine",
  "project-detail": "Project",
  "goal-detail": "Goal",
  "run-detail": "Run",
};
```

and in `durableParamsFor`:

```ts
  if (/^teams-(issue|routine|project|goal|run)-detail:/.test(panelId)) return ["panel", "detailId"];
```

- [ ] **Step 5: Widen the URL helper** (`shellNav.ts:36`)

```ts
export function teamsDetailUrl(
  kind: "issue" | "routine" | "project" | "goal" | "run",
  id: string,
): string {
  return `/chat?panel=teams-${kind}-detail&detailId=${encodeURIComponent(id)}`;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/TeamsPanelRouter.detail.test.tsx`
Expected: PASS (all cases including the new one).

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/workspace/panelRegistry.ts apps/frontend/src/components/teams/TeamsPanelRouter.tsx apps/frontend/src/lib/shellNav.ts apps/frontend/src/__tests__/teams/TeamsPanelRouter.detail.test.tsx
git commit -m "feat(teams): wire run-detail into the panel grammar (?panel=teams-run-detail)"
```

---

### Task 5: Entry links — every heartbeat-run row opens the detail panel

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/IssueRunsSection.tsx`
- Modify: `apps/frontend/src/components/teams/inbox/FailedRunRow.tsx`
- Modify: `apps/frontend/src/components/teams/dashboard/ActiveAgentsPanel.tsx`
- Tests: `apps/frontend/src/__tests__/teams/IssueRunsSection.test.tsx`, `apps/frontend/src/__tests__/teams/FailedRunRow.test.tsx` (extend both)

**Interfaces:**
- Consumes: `teamsDetailUrl("run", id)` + `handleShellLinkClick` from `@/lib/shellNav` (Task 4).
- Produces: user-visible links; no new exports.
- **Do not touch `RoutineDetailPanel` run history** (routine-run ids are not heartbeat-run ids — see Global Constraints).

- [ ] **Step 1: Extend the two tests.**

In `IssueRunsSection.test.tsx`, add (following the file's existing render/mock pattern):

```tsx
it("links each run row to the run-detail panel", () => {
  // reuse the file's existing successful-fetch setup for one run row
  // (copy the arrange block from the nearest existing rendering test)
  const link = screen.getByRole("link", { name: /view run/i });
  expect(link).toHaveAttribute(
    "href",
    "/chat?panel=teams-run-detail&detailId=run_1",
  );
});
```

In `FailedRunRow.test.tsx`, add:

```tsx
it("renders a View link to the run-detail panel", () => {
  render(
    <FailedRunRow
      run={{ id: "run_9", agentId: "agt_1", status: "failed", startedAt: null, completedAt: null, failureReason: null }}
      agentName="Lens"
      onDismiss={() => {}}
    />,
  );
  expect(screen.getByRole("link", { name: "View" })).toHaveAttribute(
    "href",
    "/chat?panel=teams-run-detail&detailId=run_9",
  );
});
```

- [ ] **Step 2: Run both to verify the new cases fail**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/IssueRunsSection.test.tsx`
Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/FailedRunRow.test.tsx`
Expected: new cases FAIL (no links exist).

- [ ] **Step 3: Link IssueRunsSection rows.** Add imports:

```ts
import { teamsDetailUrl, handleShellLinkClick } from "@/lib/shellNav";
```

In the row `<li>`, after the `timeAgo` span, add:

```tsx
              <a
                href={teamsDetailUrl("run", r.runId)}
                onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("run", r.runId))}
                aria-label={`View run ${r.runId}`}
                className="text-xs hover:underline"
                style={{ color: "var(--forest)" }}
              >
                View run
              </a>
```

- [ ] **Step 4: Add the View link to FailedRunRow.** Add the same import; inside the actions `<div className="flex items-center gap-2">`, before the Retry button:

```tsx
        <a
          href={teamsDetailUrl("run", run.id)}
          onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("run", run.id))}
          className="text-xs hover:underline"
          style={{ color: "var(--forest)" }}
        >
          View
        </a>
```

- [ ] **Step 5: Link the dashboard cards.** In `ActiveAgentsPanel.tsx`'s `AgentRunCard`, next to the existing linked-task chip (which already imports `teamsDetailUrl`/`Link` machinery — match the issue-chip pattern exactly), add a run link in the card header row:

```tsx
        <a
          href={teamsDetailUrl("run", run.id)}
          onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("run", run.id))}
          className="flex-shrink-0 text-[11px] hover:underline"
          style={{ color: "var(--mute)" }}
        >
          Details
        </a>
```

(Place it inside the header `<div className="flex items-start justify-between gap-2">`, as a sibling after the identity block. If `handleShellLinkClick` is not already imported there, add it to the existing `@/lib/shellNav` import.)

- [ ] **Step 6: Run the touched test files**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/IssueRunsSection.test.tsx`
Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/FailedRunRow.test.tsx`
Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/DashboardPanel.test.tsx`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/teams/issues/IssueRunsSection.tsx apps/frontend/src/components/teams/inbox/FailedRunRow.tsx apps/frontend/src/components/teams/dashboard/ActiveAgentsPanel.tsx apps/frontend/src/__tests__/teams/IssueRunsSection.test.tsx apps/frontend/src/__tests__/teams/FailedRunRow.test.tsx
git commit -m "feat(teams): link issue runs, inbox failed runs, and dashboard cards to run detail"
```

---

## Phase 2 — Run actions

### Task 6: BFF cancel route + wake payload field

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (after `get_run_events`)
- Modify: `apps/backend/routers/teams/runs.py`
- Modify: `apps/backend/routers/teams/schemas.py` (`WakeAgentBody`, line 59)
- Tests: `apps/backend/tests/unit/routers/teams/test_runs.py`, `apps/backend/tests/unit/routers/teams/test_schemas.py`

**Interfaces:**
- Consumes: existing wake route in `agents.py` (no change needed there — it already forwards `body.model_dump(exclude_none=True, by_alias=True)`).
- Produces: `POST /api/v1/teams/heartbeat-runs/{run_id}/cancel` (empty body) → upstream run row verbatim; `WakeAgentBody` gains `payload: Optional[dict]` so the frontend can send `{reason: "retry_failed_run", payload: {issueId: …}}`.

- [ ] **Step 1: Write the failing tests.** Append to `test_runs.py`:

```python
def test_cancel_run(client, monkeypatch):
    """`POST /teams/heartbeat-runs/{id}/cancel` proxies upstream cancel and
    returns the cancelled run row verbatim."""
    admin = MagicMock()
    admin.cancel_heartbeat_run = AsyncMock(
        return_value={"id": "run_1", "status": "cancelled"}
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/heartbeat-runs/run_1/cancel",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    admin.cancel_heartbeat_run.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
    )
```

Append to `test_schemas.py` (match its existing test style):

```python
def test_wake_agent_body_accepts_payload():
    from routers.teams.schemas import WakeAgentBody

    body = WakeAgentBody(
        source="on_demand",
        reason="retry_failed_run",
        payload={"issueId": "iss_1", "taskKey": "ADM-42"},
    )
    dumped = body.model_dump(exclude_none=True, by_alias=True)
    assert dumped["payload"] == {"issueId": "iss_1", "taskKey": "ADM-42"}


def test_wake_agent_body_still_rejects_unknown_fields():
    import pytest as _pytest
    from pydantic import ValidationError
    from routers.teams.schemas import WakeAgentBody

    with _pytest.raises(ValidationError):
        WakeAgentBody(source="on_demand", adapterConfig={"url": "x"})
```

- [ ] **Step 2: Run to verify failures**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_runs.py tests/unit/routers/teams/test_schemas.py -v`
Expected: `test_cancel_run` FAILS (404); `test_wake_agent_body_accepts_payload` FAILS (extra="forbid" rejects `payload`); the unknown-fields test PASSES already (keep it as a regression guard).

- [ ] **Step 3: Add the client method** (after `get_run_events`)

```python
    async def cancel_heartbeat_run(
        self,
        *,
        session_cookie: str,
        run_id: str,
    ) -> dict | None:
        """Cancel a queued/running heartbeat run.

        Maps to ``POST /api/heartbeat-runs/{runId}/cancel`` (board-actor
        gated upstream; the BFF's per-user session cookie is a board actor).
        Upstream returns the cancelled run row, or JSON null when the run
        was already terminal.
        """
        return await self._post(
            f"/api/heartbeat-runs/{run_id}/cancel",
            session_cookie=session_cookie,
            json={},
        )
```

(Verified: the private helper is `async def _post(self, path: str, json: dict, session_cookie: str, idempotency_key: Optional[str] = None)` at `paperclip_admin_client.py:252` — the keyword call above matches it.)

- [ ] **Step 4: Add the route** (append to `runs.py`)

```python
@router.post("/heartbeat-runs/{run_id}/cancel")
async def cancel_run(run_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Cancel a queued/running heartbeat run.

    Maps to upstream ``POST /api/heartbeat-runs/{runId}/cancel``. Stopping a
    run never *starts* spend, so no trial-runway gate here (unlike wakeup).
    """
    return await _agents._admin().cancel_heartbeat_run(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
    )
```

- [ ] **Step 5: Add `payload` to `WakeAgentBody`** (`schemas.py:59`)

```python
class WakeAgentBody(_Strict):
    # Mirrors upstream ``wakeAgentSchema``: every field is optional and
    # defaults server-side, so a bare wake is valid. The failed-run Retry
    # sends source="on_demand", reason="retry". ``source`` is constrained to
    # the upstream enum so the BFF rejects junk before it reaches Paperclip.
    # ``payload`` is upstream's free-form ``record(string, unknown)`` — the
    # run-detail Retry/Resume actions use it to target the original issue
    # (``{issueId, taskId, taskKey}``) or run (``{resumeFromRunId}``).
    source: Optional[str] = Field(default=None, pattern=r"^(timer|assignment|on_demand|automation)$")
    reason: Optional[str] = Field(default=None, max_length=2000)
    payload: Optional[dict] = None
```

- [ ] **Step 6: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/ -v`
Expected: all PASS (including the untouched wake/agents suites — `payload` is optional so existing callers are unaffected).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/paperclip_admin_client.py apps/backend/routers/teams/runs.py apps/backend/routers/teams/schemas.py apps/backend/tests/unit/routers/teams/test_runs.py apps/backend/tests/unit/routers/teams/test_schemas.py
git commit -m "feat(teams): heartbeat-run cancel pass-through + wake payload field"
```

---

### Task 7: Run actions on the detail panel (Cancel / Retry / Resume)

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/RunDetailPanel.tsx`
- Test: `apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx` (extend)

**Interfaces:**
- Consumes: `useTeamsApi().post`; BFF routes `/heartbeat-runs/{id}/cancel` (Task 6) and `/agents/{id}/wakeup` (existing, now payload-capable); `hb.mutate` for post-action refresh.
- Produces: three conditional buttons matching upstream's own rules (verified `AgentDetail.tsx:3137-3190`):
  - **Cancel** when status is `running`/`queued`/`scheduled_retry`
  - **Retry** when status is `failed`/`timed_out` → wake with `reason: "retry_failed_run"`, payload `{issueId, taskId, taskKey}` from `contextSnapshot` (omit missing keys; upstream sends NO commentId on retry)
  - **Resume** when `errorCode === "process_lost" && status === "failed"` → wake with `reason: "resume_process_lost_run"`, payload `{resumeFromRunId: run.id, issueId?, taskId?, taskKey?, commentId?}` where commentId prefers the snapshot's `wakeCommentId` over `commentId` (verified against `AgentDetail.tsx` resumePayload/retryPayload builders)
  - `triggerDetail` is deliberately omitted from our wake bodies — upstream's `handleWakeupRoute` defaults it to `"manual"` (`agents.ts:3203`), which is exactly what its own UI sends

- [ ] **Step 1: Extend the test file** with:

```tsx
it("cancels a running run", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", agentId: "agt_1", status: "running" },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({ id: "r1", status: "cancelled" });
    render(<RunDetailPanel runId="r1" />);
    const btn = screen.getByRole("button", { name: "Cancel run" });
    btn.click();
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/heartbeat-runs/r1/cancel", {}),
    );
});

it("retries a timed-out run against its source issue", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "timed_out",
        contextSnapshot: { issueId: "iss_7", taskKey: "ADM-42", other: "ignore" },
      },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({});
    render(<RunDetailPanel runId="r1" />);
    screen.getByRole("button", { name: "Retry" }).click();
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/agents/agt_1/wakeup", {
        source: "on_demand",
        reason: "retry_failed_run",
        payload: { issueId: "iss_7", taskKey: "ADM-42" },
      }),
    );
});

it("offers Resume only for process-lost failures", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", agentId: "agt_1", status: "failed", errorCode: "process_lost" },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByRole("button", { name: "Resume run" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify the new cases fail**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/RunDetailPanel.test.tsx`
Expected: 3 new FAIL, prior PASS.

- [ ] **Step 3: Implement the actions.** In `RunDetailPanel.tsx`:

Add near the other imports/helpers:

```ts
import { useState } from "react";
import { EditorialButton } from "@/components/teams/shell";

// Upstream's retry/resume wakes re-target the source issue via a payload
// subset of the run's contextSnapshot. Mirrors AgentDetail's own builders:
// retry sends issueId/taskId/taskKey only; resume additionally carries a
// commentId, preferring the snapshot's wakeCommentId over commentId.
function contextPayload(
  run: RunDetail,
  opts?: { withComment?: boolean },
): Record<string, unknown> {
  const snap = run.contextSnapshot ?? {};
  const out: Record<string, unknown> = {};
  for (const k of ["issueId", "taskId", "taskKey"]) {
    if (typeof snap[k] === "string" && snap[k]) out[k] = snap[k];
  }
  if (opts?.withComment) {
    const commentId = snap["wakeCommentId"] ?? snap["commentId"];
    if (typeof commentId === "string" && commentId) out.commentId = commentId;
  }
  return out;
}
```

Inside the component (after `run` is resolved, before the return), add:

```tsx
  const { post } = useTeamsApi();  // merge into the existing useTeamsApi() destructure
  const [acting, setActing] = useState<"cancel" | "retry" | "resume" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function act(kind: "cancel" | "retry" | "resume") {
    if (!run || acting) return;
    setActing(kind);
    setActionError(null);
    try {
      if (kind === "cancel") {
        await post(`/heartbeat-runs/${run.id}/cancel`, {});
      } else if (run.agentId) {
        await post(`/agents/${run.agentId}/wakeup`, {
          source: "on_demand",
          reason: kind === "retry" ? "retry_failed_run" : "resume_process_lost_run",
          payload:
            kind === "retry"
              ? contextPayload(run)
              : { resumeFromRunId: run.id, ...contextPayload(run, { withComment: true }) },
        });
      }
      await hb.mutate();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Action failed.");
    } finally {
      setActing(null);
    }
  }

  const canCancel = isActiveStatus(run?.status);
  const s = (run?.status ?? "").toLowerCase();
  const canRetry = s === "failed" || s === "timed_out";
  const canResume = s === "failed" && run?.errorCode === "process_lost" && !!run?.agentId;
```

In the header block, under the status line, add:

```tsx
          <div className="flex items-center gap-2">
            {canCancel && (
              <EditorialButton variant="secondary" size="sm" onClick={() => act("cancel")} disabled={acting !== null}>
                {acting === "cancel" ? "Cancelling…" : "Cancel run"}
              </EditorialButton>
            )}
            {canResume && (
              <EditorialButton variant="secondary" size="sm" onClick={() => act("resume")} disabled={acting !== null}>
                {acting === "resume" ? "Resuming…" : "Resume run"}
              </EditorialButton>
            )}
            {canRetry && run?.agentId && (
              <EditorialButton variant="secondary" size="sm" onClick={() => act("retry")} disabled={acting !== null}>
                {acting === "retry" ? "Retrying…" : "Retry"}
              </EditorialButton>
            )}
          </div>
          {actionError && (
            <p className="text-xs" style={{ color: "var(--danger)" }}>
              {actionError}
            </p>
          )}
```

- [ ] **Step 4: Run the test file**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/RunDetailPanel.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/teams/panels/RunDetailPanel.tsx apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx
git commit -m "feat(teams): cancel/retry/resume actions on the run-detail panel"
```

---

## Phase 3 — Watchdog decisions

### Task 8: BFF watchdog-decisions pass-through

**Files:**
- Modify: `apps/backend/routers/teams/schemas.py` (append)
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (after `cancel_heartbeat_run`)
- Modify: `apps/backend/routers/teams/runs.py`
- Test: `apps/backend/tests/unit/routers/teams/test_runs.py`

**Interfaces:**
- Produces: `POST /api/v1/teams/heartbeat-runs/{run_id}/watchdog-decisions` with body `WatchdogDecisionBody(decision, evaluation_issue_id?, reason?, snoozed_until?)` → upstream decision row verbatim. Wire shape is camelCase via `_Strict`'s alias generator (`evaluationIssueId`, `snoozedUntil`).

- [ ] **Step 1: Write the failing tests** (append to `test_runs.py`)

```python
def test_record_watchdog_decision(client, monkeypatch):
    """Forwards the camelCase decision body verbatim; returns the row."""
    admin = MagicMock()
    admin.record_watchdog_decision = AsyncMock(
        return_value={"id": "wd_1", "decision": "snooze"}
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/heartbeat-runs/run_1/watchdog-decisions",
        json={
            "decision": "snooze",
            "snoozedUntil": "2027-01-01T00:00:00Z",
            "reason": "long research run",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "snooze"
    admin.record_watchdog_decision.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        body={
            "decision": "snooze",
            "snoozedUntil": "2027-01-01T00:00:00Z",
            "reason": "long research run",
        },
    )


def test_watchdog_decision_rejects_bad_decision(client, monkeypatch):
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: MagicMock())
    r = client.post(
        "/api/v1/teams/heartbeat-runs/run_1/watchdog-decisions",
        json={"decision": "explode"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_runs.py -v`
Expected: both new tests FAIL (404).

- [ ] **Step 3: Add the schema** (append to `schemas.py`)

```python
class WatchdogDecisionBody(_Strict):
    # Mirrors upstream's watchdog-decisions route contract: the decision enum
    # is validated here so junk 422s at the BFF boundary; ``snoozedUntil``'s
    # required-future-when-snoozing rule stays upstream (it owns the clock).
    decision: str = Field(pattern=r"^(snooze|continue|dismissed_false_positive)$")
    evaluation_issue_id: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=4000)
    snoozed_until: Optional[str] = None
```

- [ ] **Step 4: Add the client method** (after `cancel_heartbeat_run`)

```python
    async def record_watchdog_decision(
        self,
        *,
        session_cookie: str,
        run_id: str,
        body: dict,
    ) -> dict:
        """Record an operator decision on a silent/stalled run.

        Maps to ``POST /api/heartbeat-runs/{runId}/watchdog-decisions``.
        ``body`` is the camelCase wire dict (``decision``, optional
        ``evaluationIssueId`` / ``reason`` / ``snoozedUntil``); upstream
        validates the snooze-must-be-future rule and returns the inserted
        ``heartbeat_run_watchdog_decisions`` row.
        """
        return await self._post(
            f"/api/heartbeat-runs/{run_id}/watchdog-decisions",
            session_cookie=session_cookie,
            json=body,
        )
```

- [ ] **Step 5: Add the route** (append to `runs.py`; import `WatchdogDecisionBody` from `.schemas`)

```python
@router.post("/heartbeat-runs/{run_id}/watchdog-decisions")
async def record_watchdog_decision(
    run_id: str,
    body: WatchdogDecisionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Record a watchdog decision (continue / snooze / false-positive) for a
    silent run. Maps to upstream ``POST /api/heartbeat-runs/{runId}/watchdog-decisions``."""
    return await _agents._admin().record_watchdog_decision(
        run_id=run_id,
        session_cookie=ctx.session_cookie,
        body=body.model_dump(exclude_none=True, by_alias=True),
    )
```

- [ ] **Step 6: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_runs.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/teams/schemas.py apps/backend/core/services/paperclip_admin_client.py apps/backend/routers/teams/runs.py apps/backend/tests/unit/routers/teams/test_runs.py
git commit -m "feat(teams): watchdog-decision pass-through for silent runs"
```

---

### Task 9: Silence banner + decision buttons on the detail panel

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/RunDetailPanel.tsx`
- Test: `apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx` (extend)

**Interfaces:**
- Consumes: `run.outputSilence` (injected by the upstream single-run GET), `formatSilenceAgeMs` from `@/components/teams/shared/lib/liveRuns`, BFF route from Task 8.
- Produces: a warning banner when `outputSilence.level` is `"suspicious"` or `"critical"`, with three buttons mirroring upstream's ledger: **Continue monitoring** (`decision: "continue"`), **Snooze 1h** (`decision: "snooze"`, `snoozedUntil: now + 1h ISO`), **Mark false positive** (`decision: "dismissed_false_positive"`). Thresholds and copy come from upstream's `outputSilence` — display-only mirror, no local liveness logic.

- [ ] **Step 1: Extend the test file**

```tsx
it("shows the silence watchdog banner and records a snooze", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "running",
        outputSilence: { level: "critical", silenceAgeMs: 4 * 3600 * 1000 },
      },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({ id: "wd_1", decision: "snooze" });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText(/silent for/i)).toBeInTheDocument();
    screen.getByRole("button", { name: "Snooze 1h" }).click();
    await vi.waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/heartbeat-runs/r1/watchdog-decisions",
        expect.objectContaining({ decision: "snooze", snoozedUntil: expect.any(String) }),
      );
    });
});

it("hides the watchdog banner when silence level is ok", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", status: "running", outputSilence: { level: "ok" } },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.queryByText(/silent for/i)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify the new cases fail**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/RunDetailPanel.test.tsx`
Expected: 2 new FAIL.

- [ ] **Step 3: Implement the banner.** In `RunDetailPanel.tsx`, add the import:

```ts
import { formatSilenceAgeMs } from "@/components/teams/shared/lib/liveRuns";
```

(Verified: `export function formatSilenceAgeMs(ms: number | null | undefined): string | null` at `liveRuns.ts:71`.)

Add state + handler beside the Phase 2 action code:

```tsx
  const [wdActing, setWdActing] = useState(false);
  const silence = run?.outputSilence ?? null;
  const silenceLevel = silence?.level ?? null;
  const showWatchdog = silenceLevel === "suspicious" || silenceLevel === "critical";

  async function decideWatchdog(decision: "continue" | "snooze" | "dismissed_false_positive") {
    if (!run || wdActing) return;
    setWdActing(true);
    setActionError(null);
    try {
      const body: Record<string, unknown> = { decision };
      if (decision === "snooze") {
        body.snoozedUntil = new Date(Date.now() + 60 * 60 * 1000).toISOString();
        body.reason = "Snoozed from run detail";
      }
      await post(`/heartbeat-runs/${run.id}/watchdog-decisions`, body);
      await hb.mutate();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Watchdog decision failed.");
    } finally {
      setWdActing(false);
    }
  }
```

Render the banner in `<main>`, directly above the failure block:

```tsx
        {showWatchdog && (
          <section
            className="flex flex-col gap-2 rounded px-3 py-2 text-sm"
            style={{
              border: `1px solid color-mix(in srgb, ${silenceLevel === "critical" ? "var(--danger)" : "var(--warn)"} 35%, var(--edge))`,
              background: `color-mix(in srgb, ${silenceLevel === "critical" ? "var(--danger)" : "var(--warn)"} 8%, transparent)`,
            }}
          >
            <p className="font-medium">
              {silenceLevel === "critical" ? "Stale-run watchdog alert" : "Output silence warning"}
            </p>
            <p className="text-xs" style={{ color: "var(--mute)" }}>
              This run has been silent for {formatSilenceAgeMs(silence?.silenceAgeMs ?? null) ?? "an extended period"}.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <EditorialButton variant="secondary" size="sm" disabled={wdActing} onClick={() => decideWatchdog("continue")}>
                Continue monitoring
              </EditorialButton>
              <EditorialButton variant="secondary" size="sm" disabled={wdActing} onClick={() => decideWatchdog("snooze")}>
                Snooze 1h
              </EditorialButton>
              <EditorialButton variant="secondary" size="sm" disabled={wdActing} onClick={() => decideWatchdog("dismissed_false_positive")}>
                Mark false positive
              </EditorialButton>
            </div>
          </section>
        )}
```

- [ ] **Step 4: Run the test file**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/teams/RunDetailPanel.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Full-suite gate before wrapping the branch**

Run: `cd apps/backend && uv run pytest tests/ -q`
Run: `cd apps/frontend && pnpm run lint && pnpm exec tsc --noEmit`
Expected: clean. (Frontend vitest stays single-file; lint + tsc are the whole-tree gates.)

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/teams/panels/RunDetailPanel.tsx apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx
git commit -m "feat(teams): output-silence watchdog banner + decisions on run detail"
```

---

## Out of scope (deliberate)

- **Event-driven refresh** — `TeamsEventsProvider.tsx:46-47` maps `teams.heartbeat.run.event`/`.log` to no-op invalidation lists. The detail panel polls on the 15s beat like every other run surface; filling in those arrays is a follow-up once the panel proves out.
- **Routine-run drill-down** — different upstream entity (`routine_runs`); would need its own BFF route and panel kind.
- **nextOffset-walking full-log pagination** — the tail-256KiB read covers the real cases; revisit only if users hit the truncation notice in practice.
- **Watchdog surfacing on IssueDetailPage** — upstream shows the silent-run banner in the issue run ledger too; ours stays on run detail for v1 (the issue page's run rows now link there).
