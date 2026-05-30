# Teams Polish (clean trio) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Three small frontend-only `/teams` parity wins — edit existing Goals, a standalone approval-detail page, and a type filter on the Activity feed.

**Architecture:** All frontend; the BFF endpoints (`PATCH /goals/{id}`, `GET /approvals/{id}`, `GET /activity`) already exist. Each item is independent.

**Tech Stack:** Next.js 16 App Router, React 19, `useTeamsApi` (`read`/`post`/`patch`), SWR, Tailwind + CSS-var theming.

Conventions: frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. SWR key for `read("/x")` is `/teams/x`. New tests go in `src/__tests__/teams/`.

---

## Task 1: H1 — Goal edit (status chip + EditGoalDialog)

**Files:** Modify `apps/frontend/src/components/teams/panels/GoalsPanel.tsx`; Test `apps/frontend/src/__tests__/teams/GoalsPanel.test.tsx` (new, or extend if one exists).

Read the current `GoalsPanel.tsx` first — it has `Goal = {id, title, description?, parent_id?, status?}`, a recursive `GoalRow`, a `creating` state, and an inline `CreateGoalDialog` that `post`s `/goals`. `useTeamsApi` exposes `patch`.

- [ ] **Step 1: Failing test** — `src/__tests__/teams/GoalsPanel.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const read = vi.fn();
const patch = vi.fn().mockResolvedValue({});
const post = vi.fn().mockResolvedValue({});
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read, patch, post }) }));

import { GoalsPanel } from "@/components/teams/panels/GoalsPanel";

beforeEach(() => {
  read.mockReset();
  patch.mockClear();
  read.mockReturnValue({
    data: { goals: [{ id: "g1", title: "Ship v1", description: "the thing", status: "planned" }] },
    isLoading: false,
    error: null,
    mutate: vi.fn(),
  });
});

describe("GoalsPanel", () => {
  it("shows a status chip for each goal", () => {
    render(<GoalsPanel />);
    expect(screen.getByText("planned")).toBeInTheDocument();
    expect(screen.getByText("Ship v1")).toBeInTheDocument();
  });

  it("opens EditGoalDialog pre-filled and patches title/description/status on save", async () => {
    render(<GoalsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    const title = screen.getByDisplayValue("Ship v1") as HTMLInputElement;
    expect(title).toBeInTheDocument();
    const statusSelect = screen.getByLabelText(/status/i) as HTMLSelectElement;
    fireEvent.change(statusSelect, { target: { value: "active" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/goals/g1", {
        title: "Ship v1",
        description: "the thing",
        status: "active",
      }),
    );
  });
});
```

- [ ] **Step 2: Run → fail.** `pnpm test -- GoalsPanel`

- [ ] **Step 3: Add the status-color helper + chip + edit button to `GoalRow`.** Replace the existing `GoalRow` function with:
```tsx
function goalStatusColor(status?: string): string {
  const s = (status ?? "").toLowerCase();
  if (s === "active" || s === "achieved") return "var(--forest)";
  return "var(--mute)";
}

function GoalRow({
  node,
  depth,
  onEdit,
}: {
  node: GoalNode;
  depth: number;
  onEdit: (goal: Goal) => void;
}) {
  return (
    <>
      <li
        className="p-3 border-b border-[var(--edge)] last:border-b-0 flex items-start gap-2"
        style={{ paddingLeft: `${depth * 1.5 + 0.75}rem` }}
      >
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-[var(--ink)]">{node.title}</span>
            {node.status && (
              <span
                className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded flex-shrink-0"
                style={{ background: "var(--edge)", color: goalStatusColor(node.status) }}
              >
                {node.status}
              </span>
            )}
          </div>
          {node.description && (
            <div className="text-xs text-[var(--mute)] mt-0.5">{node.description}</div>
          )}
        </div>
        <EditorialButton variant="secondary" size="sm" onClick={() => onEdit(node)}>
          Edit
        </EditorialButton>
      </li>
      {node.children.map((c) => (
        <GoalRow key={c.id} node={c} depth={depth + 1} onEdit={onEdit} />
      ))}
    </>
  );
}
```

- [ ] **Step 4: Wire edit state in `GoalsPanel`.** Inside `GoalsPanel`, add `const [editing, setEditing] = useState<Goal | null>(null);` next to `creating`. Change the tree render `<GoalRow key={root.id} node={root} depth={0} />` → `<GoalRow key={root.id} node={root} depth={0} onEdit={setEditing} />`. After the `{creating && (<CreateGoalDialog … />)}` block, add:
```tsx
      {editing && (
        <EditGoalDialog
          goal={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            mutate();
          }}
        />
      )}
```

- [ ] **Step 5: Add `EditGoalDialog`** at the bottom of the file (mirrors `CreateGoalDialog`, uses `patch`):
```tsx
// upstream GOAL_STATUSES
const GOAL_STATUSES = ["planned", "active", "achieved", "cancelled"];

function EditGoalDialog({
  goal,
  onClose,
  onSaved,
}: {
  goal: Goal;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { patch } = useTeamsApi();
  const [title, setTitle] = useState(goal.title);
  const [description, setDescription] = useState(goal.description ?? "");
  const [status, setStatus] = useState(goal.status ?? "planned");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      await patch(`/goals/${goal.id}`, { title, description, status });
      onSaved();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  return (
    <EditorialDialog open onOpenChange={(o) => !o && onClose()} title="Edit goal">
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Status</span>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          >
            {GOAL_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        {err && (
          <div className="text-[var(--danger)] text-sm border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 mt-2">
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton size="sm" onClick={submit} disabled={submitting || !title}>
            {submitting ? "Saving…" : "Save"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
```

- [ ] **Step 6: Run → pass.** `pnpm test -- GoalsPanel`. Then `pnpm exec tsc --noEmit` (clean) + `pnpm exec eslint src/components/teams/panels/GoalsPanel.tsx` (clean).

- [ ] **Step 7: Commit** — `git commit -m "feat(teams): edit goals — status chip + EditGoalDialog (H1)"`

---

## Task 2: H2 — Approval-detail page

**Files:** Modify `apps/frontend/src/components/teams/shared/types.ts` (add `ApprovalDetail`), `apps/frontend/src/components/teams/inbox/ApprovalRow.tsx` (View link); Create `apps/frontend/src/components/teams/panels/ApprovalDetailPanel.tsx` + `apps/frontend/src/app/teams/approvals/[approvalId]/page.tsx`; Test `apps/frontend/src/__tests__/teams/ApprovalDetailPanel.test.tsx` (new) + extend the existing `ApprovalRow` test if present.

The BFF `GET /approvals/{id}` returns the upstream Approval verbatim: `{ id, companyId, type, requestedByAgentId, requestedByUserId, status, payload, decisionNote, decidedByUserId, decidedAt, createdAt, updatedAt }` (no `title`). `useApprovalMutations()` gives `approve(id)` / `reject(id, reason)`. The run-detail route `app/teams/agents/[agentId]/runs/[runId]/page.tsx` is the pattern to mirror.

- [ ] **Step 1: Add the `ApprovalDetail` type** to `shared/types.ts` (near the existing `Approval` interface):
```ts
/** Full approval object from GET /approvals/{id} (upstream Approval subset). `status` is a free string (more values than the row union). */
export interface ApprovalDetail {
  id: string;
  type?: string | null;
  status: string;
  requestedByAgentId?: string | null;
  requestedByUserId?: string | null;
  payload?: Record<string, unknown> | null;
  decisionNote?: string | null;
  decidedAt?: string | null;
  createdAt?: string | null;
}
```

- [ ] **Step 2: Failing test** — `src/__tests__/teams/ApprovalDetailPanel.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const read = vi.fn();
const approve = vi.fn().mockResolvedValue(undefined);
const reject = vi.fn().mockResolvedValue(undefined);
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

import { ApprovalDetailPanel } from "@/components/teams/panels/ApprovalDetailPanel";

function mockReads(approval: Record<string, unknown>) {
  read.mockImplementation((path: string) => {
    if (path === "/approvals/a1")
      return { data: approval, isLoading: false, error: null, mutate: vi.fn() };
    if (path === "/agents")
      return { data: { agents: [{ id: "ag1", name: "Atlas" }] }, isLoading: false, error: null, mutate: vi.fn() };
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

beforeEach(() => { read.mockReset(); approve.mockClear(); reject.mockClear(); });

describe("ApprovalDetailPanel", () => {
  it("renders type, status, requester, and toggles the payload", () => {
    mockReads({ id: "a1", type: "exec_command", status: "pending",
      requestedByAgentId: "ag1", payload: { cmd: "ls -la" }, createdAt: "2026-05-01T00:00:00Z" });
    render(<ApprovalDetailPanel approvalId="a1" />);
    expect(screen.getByText(/exec command/i)).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
    expect(screen.queryByText(/ls -la/)).toBeNull(); // payload hidden until toggled
    fireEvent.click(screen.getByRole("button", { name: /payload/i }));
    expect(screen.getByText(/ls -la/)).toBeInTheDocument();
  });

  it("approves a pending approval", async () => {
    mockReads({ id: "a1", type: "exec_command", status: "pending", payload: {} });
    render(<ApprovalDetailPanel approvalId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a1"));
  });

  it("hides actions for a resolved approval", () => {
    mockReads({ id: "a1", type: "exec_command", status: "approved", payload: {} });
    render(<ApprovalDetailPanel approvalId="a1" />);
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run → fail.** `pnpm test -- ApprovalDetailPanel`

- [ ] **Step 4: Create `ApprovalDetailPanel.tsx`:**
```tsx
// apps/frontend/src/components/teams/panels/ApprovalDetailPanel.tsx
"use client";

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { EditorialButton } from "@/components/teams/shell";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { ApprovalDetail, CompanyAgent } from "@/components/teams/shared/types";

function approvalLabel(type?: string | null): string {
  return type ? type.replace(/_/g, " ") : "Approval";
}

export function ApprovalDetailPanel({ approvalId }: { approvalId: string }) {
  const { read } = useTeamsApi();
  const { data, isLoading, mutate } = read<ApprovalDetail>(`/approvals/${approvalId}`);
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { approve, reject } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [showRaw, setShowRaw] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (isLoading) return <div className="p-8">Loading…</div>;
  if (!data) return <div className="p-8 text-[var(--mute)]">Approval not found.</div>;

  const agents = agentsData?.agents ?? [];
  const requester = data.requestedByAgentId
    ? agents.find((a) => a.id === data.requestedByAgentId)?.name ?? data.requestedByAgentId
    : data.requestedByUserId ?? "—";
  const isPending = data.status === "pending";

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await mutate();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-8 max-w-3xl flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold capitalize">{approvalLabel(data.type)}</h1>
        <span
          className="text-xs capitalize px-2 py-0.5 rounded"
          style={{ background: "var(--edge)", color: isPending ? "var(--forest)" : "var(--mute)" }}
        >
          {data.status}
        </span>
      </div>
      <div className="text-xs text-[var(--mute)] font-mono">{data.id}</div>
      <div className="text-sm text-[var(--ink)]">
        Requested by <span className="font-medium">{requester}</span>
        {data.createdAt && <> · {timeAgo(data.createdAt)}</>}
      </div>
      {data.decisionNote && (
        <div className="text-sm">
          <span className="text-[var(--mute)]">Decision note: </span>
          {data.decisionNote}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <button
          type="button"
          onClick={() => setShowRaw((v) => !v)}
          className="text-xs self-start"
          style={{ color: "var(--forest)" }}
        >
          {showRaw ? "Hide" : "Show"} request payload
        </button>
        {showRaw && (
          <pre className="bg-[var(--cream-deeper)] border border-[var(--edge)] rounded p-3 text-xs overflow-x-auto whitespace-pre-wrap">
            {JSON.stringify(data.payload ?? {}, null, 2)}
          </pre>
        )}
      </div>
      {isPending && !rejecting && (
        <div className="flex gap-2">
          <EditorialButton size="sm" disabled={busy} onClick={() => act(() => approve(data.id))}>
            Approve
          </EditorialButton>
          <EditorialButton variant="destructive" size="sm" disabled={busy} onClick={() => setRejecting(true)}>
            Reject
          </EditorialButton>
        </div>
      )}
      {isPending && rejecting && (
        <div className="flex items-center gap-2">
          <input
            autoFocus
            aria-label="Reason for rejection"
            placeholder="Reason for rejection"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            className="flex-1 min-w-0 bg-transparent outline-none"
            style={{ borderBottom: "1px solid var(--edge)", color: "var(--ink)" }}
          />
          <EditorialButton
            variant="destructive"
            size="sm"
            disabled={busy || reason.trim() === ""}
            onClick={() => act(async () => { await reject(data.id, reason.trim()); })}
          >
            Confirm reject
          </EditorialButton>
          <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => { setRejecting(false); setReason(""); }}>
            Cancel
          </EditorialButton>
        </div>
      )}
      {error && <span className="text-xs text-[var(--danger)]">{error}</span>}
    </div>
  );
}
```

- [ ] **Step 5: Create the route page** `app/teams/approvals/[approvalId]/page.tsx`:
```tsx
"use client";

import { useParams } from "next/navigation";
import { ApprovalDetailPanel } from "@/components/teams/panels/ApprovalDetailPanel";

export default function Page() {
  const { approvalId } = useParams<{ approvalId: string }>();
  return <ApprovalDetailPanel approvalId={approvalId!} />;
}
```

- [ ] **Step 6: Add a "View" link to `ApprovalRow.tsx`** — in the header `<div className="flex items-center gap-2">`, immediately AFTER the `{approval.createdAt && (<span>…timeAgo…</span>)}` block and BEFORE the `{isPending && !rejecting && (…)}` block, insert:
```tsx
        <a
          href={`/teams/approvals/${approval.id}`}
          className="text-xs hover:underline"
          style={{ color: "var(--forest)" }}
        >
          View
        </a>
```
(`IssueApprovalsSection` renders `ApprovalRow`, so this one change adds the link in both the inbox band and the issue-detail approvals section.)

- [ ] **Step 7: Keep the existing `ApprovalRow` test green + add a View assertion.** Run `pnpm test -- ApprovalRow`; if a test file exists, add a case asserting the row renders a link with `href="/teams/approvals/{id}"`; if the added link breaks a layout/query assertion, adjust the test (don't weaken action assertions).

- [ ] **Step 8: Run → pass.** `pnpm test -- ApprovalDetailPanel ApprovalRow`. Then `pnpm exec tsc --noEmit` (clean) + `pnpm exec eslint src/components/teams/panels/ApprovalDetailPanel.tsx src/components/teams/inbox/ApprovalRow.tsx "src/app/teams/approvals/[approvalId]/page.tsx"` (clean).

- [ ] **Step 9: Commit** — `git commit -m "feat(teams): approval-detail page + View links (H2)"`

---

## Task 3: H3 — Activity type-filter

**Files:** Modify `apps/frontend/src/components/teams/panels/ActivityPanel.tsx`; Test `apps/frontend/src/__tests__/teams/ActivityPanel.test.tsx` (new).

Read the current `ActivityPanel.tsx` — `ActivityEvent = {id, type, actor?, target?, createdAt?, description?}`, reads `/activity` → `{events}`, renders a flat list keyed on `e.type`.

- [ ] **Step 1: Failing test** — `src/__tests__/teams/ActivityPanel.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { ActivityPanel } from "@/components/teams/panels/ActivityPanel";

beforeEach(() => {
  read.mockReset();
  read.mockReturnValue({
    data: { events: [
      { id: "e1", type: "issue.created", description: "made an issue" },
      { id: "e2", type: "run.failed", description: "a run failed" },
    ] },
    isLoading: false, error: null,
  });
});

describe("ActivityPanel", () => {
  it("renders a type filter and shows all events by default", () => {
    render(<ActivityPanel />);
    expect(screen.getByText("made an issue")).toBeInTheDocument();
    expect(screen.getByText("a run failed")).toBeInTheDocument();
    const select = screen.getByLabelText(/filter by type/i) as HTMLSelectElement;
    expect(select).toBeInTheDocument();
  });

  it("filters the list to the selected type", () => {
    render(<ActivityPanel />);
    fireEvent.change(screen.getByLabelText(/filter by type/i), { target: { value: "run.failed" } });
    expect(screen.getByText("a run failed")).toBeInTheDocument();
    expect(screen.queryByText("made an issue")).toBeNull();
  });
});
```

- [ ] **Step 2: Run → fail.** `pnpm test -- ActivityPanel`

- [ ] **Step 3: Implement** — update the imports to `import { useMemo, useState } from "react";` (keep `useTeamsApi`), and replace the body so the hooks run before the early returns. The `ActivityEvent` interface stays. New component body:
```tsx
export function ActivityPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ events: ActivityEvent[] }>("/activity");
  const [typeFilter, setTypeFilter] = useState("all");

  const events = useMemo(() => data?.events ?? [], [data]);
  const types = useMemo(
    () => Array.from(new Set(events.map((e) => e.type).filter(Boolean))).sort(),
    [events],
  );
  const filtered = typeFilter === "all" ? events : events.filter((e) => e.type === typeFilter);

  if (isLoading)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <div className="flex items-center justify-between mb-6 gap-4">
        <h1 className="font-editorial-serif text-4xl text-[var(--ink)]">Activity</h1>
        {types.length > 0 && (
          <select
            aria-label="Filter by type"
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="border border-[var(--edge)] bg-[var(--cream)] rounded px-2 py-1 text-sm text-[var(--ink)]"
          >
            <option value="all">All types</option>
            {types.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        )}
      </div>
      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {filtered.map((e) => (
          <li key={e.id} className="p-4 flex justify-between items-start gap-4">
            <div>
              <div className="text-xs uppercase tracking-wide text-[var(--mute)]">{e.type}</div>
              <div className="text-sm text-[var(--ink)] mt-0.5">
                {e.description ?? `${e.actor ?? "?"} → ${e.target ?? "?"}`}
              </div>
            </div>
            {e.createdAt && (
              <div className="text-xs text-[var(--mute)] whitespace-nowrap font-mono">{e.createdAt}</div>
            )}
          </li>
        ))}
        {filtered.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            {events.length === 0 ? "No activity yet." : "No activity of this type."}
          </li>
        )}
      </ul>
    </div>
  );
}
```

- [ ] **Step 4: Run → pass.** `pnpm test -- ActivityPanel`. Then `pnpm exec tsc --noEmit` (clean) + `pnpm exec eslint src/components/teams/panels/ActivityPanel.tsx` (clean).

- [ ] **Step 5: Commit** — `git commit -m "feat(teams): activity type filter (H3)"`

---

## Task 4: Full verification + roadmap

- [ ] **Step 1:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 2:** `pnpm exec eslint src/components/teams/panels/GoalsPanel.tsx src/components/teams/panels/ApprovalDetailPanel.tsx src/components/teams/panels/ActivityPanel.tsx src/components/teams/inbox/ApprovalRow.tsx` (0 errors).
- [ ] **Step 3:** `pnpm exec vitest run` — H tests pass; only the pre-existing local-env-only files may fail (BotSetupWizard, useChatGPTOAuth, CreditsPanel, ChatGPTOAuthStep). Confirm no regression in `GoalsPanel`, `ApprovalDetailPanel`, `ApprovalRow`, `ActivityPanel`, and the inbox band/IssueApprovalsSection tests. (vitest may hang at teardown — read per-file results.)
- [ ] **Step 4:** `pnpm run build` — "✓ Compiled successfully" (a later Clerk-prerender failure on `/admin/health`/marketing pages is the known env-only issue, not a code error).
- [ ] **Step 5:** Roadmap: flip row H to **Done** (note the trio shipped; dashboard-date-range dropped + routine-run-history deferred remain noted). Commit.

---

## Self-Review
- Spec coverage: H1 status chip + EditGoalDialog + patch (Task 1); H2 ApprovalDetail type + panel + route + View link in ApprovalRow (covers band + IssueApprovalsSection) (Task 2); H3 type select + client filter (Task 3); verification + roadmap (Task 4). Deferred items (dashboard date-range, routine runs, goal level, approval comments/linked-issues, server-side activity filters) correctly omitted.
- Type consistency: `Goal`/`GoalNode` unchanged; `onEdit: (goal: Goal) => void` threaded through GoalRow + GoalsPanel + EditGoalDialog `{goal,onClose,onSaved}`. `ApprovalDetail` shape used identically in the panel + test. `useTeamsApi().patch` (H1) and `.read` (H2/H3) match the hook's real surface. `useApprovalMutations` `approve(id)`/`reject(id,reason)` match the real hook.
- Hooks order: ApprovalDetailPanel + ActivityPanel call all hooks (`read`×N, `useState`, `useMemo`) before any early return. OK.
- Safety: payload rendered as `JSON.stringify` in a `<pre>` (no HTML injection); approval `status` typed as free string, actions gated on `=== "pending"`.
- Placeholder scan: none.
