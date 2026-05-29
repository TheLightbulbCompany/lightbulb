# Teams Inbox Mixed Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface pending approvals + failed heartbeat runs in `/teams/inbox` as a collapsible "Needs attention" band with inline actions, and retire the standalone Approvals panel.

**Architecture:** A new `NeedsAttentionBand` renders above the inbox toolbar. It composes three new hooks — `useInboxTriage` (reads the existing `/teams/approvals` + `/teams/inbox/runs` BFF endpoints), `useApprovalMutations` (approve/reject), and `useAcknowledgedRuns` (per-user localStorage acknowledge for failed runs). No backend changes: the BFF endpoints and types already exist. Realtime gets two key-map additions; the Approvals sidebar entry + panel are removed with a redirect.

**Tech Stack:** Next.js 16 App Router, React 19, SWR, TypeScript, Tailwind v4, Vitest + Testing Library. All work is frontend under `apps/frontend/`.

**Spec:** `docs/superpowers/specs/2026-05-29-teams-inbox-mixed-triage-design.md`

---

## Conventions for every task

- Run commands from `apps/frontend/`.
- Run a single test file with: `pnpm test -- <pathOrSubstring>`.
- TypeScript check: `pnpm exec tsc --noEmit`.
- Lint a file: `pnpm exec eslint <path>`.
- All new `.ts/.tsx` files start with a `// apps/frontend/<relpath>` first-line comment (repo convention seen across `src/components/teams/`).
- SWR keys: `useTeamsApi().read("/x")` registers the SWR key **`/teams/x`** (the hook prepends `/teams`). Invalidate with that full key.
- Reference types live in `src/components/teams/shared/types.ts`: `Approval { id; issueId?; title?; status: "pending"|"approved"|"rejected"; createdAt?; decidedAt? }`, `HeartbeatRun { id; agentId?; status: "queued"|"running"|"succeeded"|"failed"|"cancelled"; startedAt?; completedAt?; failureReason? }`, `CompanyAgent { id; name; ... }`.

---

## File Structure

**Create:**
- `src/components/teams/inbox/NeedsAttentionBand.tsx` — collapsible container; count header; hide-when-empty; renders rows.
- `src/components/teams/inbox/ApprovalRow.tsx` — one approval; Approve (one click) / Reject (inline required reason).
- `src/components/teams/inbox/FailedRunRow.tsx` — one failed run; View link + Dismiss.
- `src/components/teams/inbox/hooks/useInboxTriage.ts` — reads + merges approvals + failed runs (minus acked).
- `src/components/teams/inbox/hooks/useApprovalMutations.ts` — approve / reject + SWR revalidation.
- `src/components/teams/inbox/hooks/useAcknowledgedRuns.ts` — per-company local ack set.
- Tests for each of the above under `src/__tests__/teams/`.

**Modify:**
- `src/components/teams/shared/lib/inboxStorage.ts` — add ack-runs + band-collapsed helpers.
- `src/components/teams/inbox/InboxPage.tsx` — mount the band.
- `src/components/teams/TeamsEventsProvider.tsx` — add realtime keys; export `EVENT_KEY_MAP` for testability.
- `src/components/teams/TeamsSidebar.tsx` — remove the `approvals` nav entry.
- `src/components/teams/TeamsPanelRouter.tsx` — remove `approvals` mapping + redirect retired keys.
- `src/__tests__/components/teams/shared/lib/inboxStorage.test.ts` — extend.
- `src/__tests__/teams/TeamsEventsProvider.test.tsx` — extend.
- `src/__tests__/components/teams/inbox/InboxPage.test.tsx` — keep green (band renders null on empty mocks).

**Delete:**
- `src/components/teams/panels/ApprovalsPanel.tsx`
- `src/__tests__/teams/ApprovalsPanel.test.tsx`

---

## Task 1: localStorage helpers for ack-runs + band-collapsed

**Files:**
- Modify: `src/components/teams/shared/lib/inboxStorage.ts`
- Test: `src/__tests__/components/teams/shared/lib/inboxStorage.test.ts` (extend)

These mirror the existing `loadReadInboxItems/saveReadInboxItems` (Set<string>) and `loadInboxViewMode/saveInboxViewMode` (single value) helpers already in the file.

- [ ] **Step 1: Write the failing tests** (append to the existing describe block / file)

```ts
import {
  loadAckRuns,
  saveAckRuns,
  loadBandCollapsed,
  saveBandCollapsed,
} from "@/components/teams/shared/lib/inboxStorage";

describe("triage storage helpers", () => {
  beforeEach(() => localStorage.clear());

  it("ack-runs round-trips a Set and isolates per company", () => {
    saveAckRuns("co1", new Set(["r1", "r2"]));
    expect(loadAckRuns("co1")).toEqual(new Set(["r1", "r2"]));
    expect(loadAckRuns("co2")).toEqual(new Set()); // per-company isolation
  });

  it("ack-runs returns empty Set when missing or malformed", () => {
    expect(loadAckRuns("missing")).toEqual(new Set());
    localStorage.setItem("paperclip:inbox:bad:ack-runs", "{not json");
    expect(loadAckRuns("bad")).toEqual(new Set());
  });

  it("band-collapsed round-trips a boolean, default false", () => {
    expect(loadBandCollapsed("co1")).toBe(false);
    saveBandCollapsed("co1", true);
    expect(loadBandCollapsed("co1")).toBe(true);
    saveBandCollapsed("co1", false);
    expect(loadBandCollapsed("co1")).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- inboxStorage`
Expected: FAIL — `loadAckRuns is not a function` (exports don't exist yet).

- [ ] **Step 3: Implement the helpers** (append to `inboxStorage.ts`, after `saveInboxViewMode`)

```ts
/**
 * Read the per-company set of acknowledged (dismissed-from-band) failed-run
 * IDs. Mirrors loadReadInboxItems. Empty Set on missing/malformed/throw.
 */
export function loadAckRuns(companyId: string): Set<string> {
  let raw: string | null;
  try {
    raw = localStorage.getItem(inboxStorageKey(companyId, "ack-runs"));
  } catch {
    return new Set();
  }
  if (!raw) return new Set();
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((v): v is string => typeof v === "string"));
  } catch {
    return new Set();
  }
}

/** Persist the acknowledged failed-run IDs as a JSON array. Swallows errors. */
export function saveAckRuns(companyId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(
      inboxStorageKey(companyId, "ack-runs"),
      JSON.stringify([...ids]),
    );
  } catch {
    // Ignore localStorage failures.
  }
}

/** Read the per-company "Needs attention" band collapsed flag. Default false. */
export function loadBandCollapsed(companyId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return localStorage.getItem(inboxStorageKey(companyId, "triage-collapsed")) === "1";
  } catch {
    return false;
  }
}

/** Persist the band collapsed flag. Swallows storage errors. */
export function saveBandCollapsed(companyId: string, collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(
      inboxStorageKey(companyId, "triage-collapsed"),
      collapsed ? "1" : "0",
    );
  } catch {
    // Swallow — Safari Private mode / quota exceeded.
  }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- inboxStorage`
Expected: PASS (existing tests + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/shared/lib/inboxStorage.ts "src/__tests__/components/teams/shared/lib/inboxStorage.test.ts"
git commit -m "feat(teams): inboxStorage helpers for failed-run ack + band collapse"
```

---

## Task 2: useAcknowledgedRuns hook

**Files:**
- Create: `src/components/teams/inbox/hooks/useAcknowledgedRuns.ts`
- Test: `src/__tests__/teams/useAcknowledgedRuns.test.tsx`

Mirrors `useReadInboxItems` (lazy init, companyId-change reset during render, cross-tab `storage` sync).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/useAcknowledgedRuns.test.tsx
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";

describe("useAcknowledgedRuns", () => {
  beforeEach(() => localStorage.clear());

  it("ack adds an id, isAcked reflects it, and it persists", () => {
    const { result } = renderHook(() => useAcknowledgedRuns("co1"));
    expect(result.current.isAcked("r1")).toBe(false);
    act(() => result.current.ack("r1"));
    expect(result.current.isAcked("r1")).toBe(true);
    // Persisted: a fresh hook for the same company sees it.
    const second = renderHook(() => useAcknowledgedRuns("co1"));
    expect(second.result.current.isAcked("r1")).toBe(true);
  });

  it("null companyId is a no-op", () => {
    const { result } = renderHook(() => useAcknowledgedRuns(null));
    act(() => result.current.ack("r1"));
    expect(result.current.isAcked("r1")).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- useAcknowledgedRuns`
Expected: FAIL — cannot find module / `useAcknowledgedRuns is not a function`.

- [ ] **Step 3: Implement the hook**

```ts
// apps/frontend/src/components/teams/inbox/hooks/useAcknowledgedRuns.ts
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  inboxStorageKey,
  loadAckRuns,
  saveAckRuns,
} from "@/components/teams/shared/lib/inboxStorage";

export interface UseAcknowledgedRunsResult {
  acknowledgedRunIds: Set<string>;
  isAcked: (id: string) => boolean;
  ack: (id: string) => void;
}

/**
 * Per-company set of acknowledged (dismissed-from-band) failed-run IDs,
 * persisted to localStorage and synced across tabs. Mirrors
 * useReadInboxItems. `null` companyId yields an empty set + no-op `ack`.
 */
export function useAcknowledgedRuns(
  companyId: string | null,
): UseAcknowledgedRunsResult {
  const [ackIds, setAckIds] = useState<Set<string>>(() =>
    companyId ? loadAckRuns(companyId) : new Set(),
  );

  // Reset inline during render when companyId changes (avoids the
  // set-state-in-effect render flash). Same pattern as useReadInboxItems.
  const [loadedCompanyId, setLoadedCompanyId] = useState<string | null>(companyId);
  if (companyId !== loadedCompanyId) {
    setLoadedCompanyId(companyId);
    setAckIds(companyId ? loadAckRuns(companyId) : new Set());
  }

  useEffect(() => {
    if (!companyId) return;
    const key = inboxStorageKey(companyId, "ack-runs");
    const handler = (event: StorageEvent) => {
      if (event.key !== key) return;
      setAckIds(loadAckRuns(companyId));
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, [companyId]);

  const ack = useCallback(
    (id: string) => {
      if (!companyId) return;
      setAckIds((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        saveAckRuns(companyId, next);
        return next;
      });
    },
    [companyId],
  );

  const isAcked = useCallback((id: string) => ackIds.has(id), [ackIds]);

  return { acknowledgedRunIds: ackIds, isAcked, ack };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- useAcknowledgedRuns`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/hooks/useAcknowledgedRuns.ts src/__tests__/teams/useAcknowledgedRuns.test.tsx
git commit -m "feat(teams): useAcknowledgedRuns hook for failed-run dismiss"
```

---

## Task 3: useInboxTriage hook

**Files:**
- Create: `src/components/teams/inbox/hooks/useInboxTriage.ts`
- Test: `src/__tests__/teams/useInboxTriage.test.tsx`

Reads `/approvals` (real list endpoint, returns `{approvals: Approval[]}`) and `/inbox/runs` (failed heartbeat runs). Filters approvals to `status === "pending"`, runs to `status === "failed"` AND not acknowledged. Returns merged result + count.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/useInboxTriage.test.tsx
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";

function mockRead(approvals: unknown, runs: unknown) {
  read.mockImplementation((path: string) => {
    if (path === "/approvals") return { data: approvals, isLoading: false, error: null, mutate: vi.fn() };
    if (path === "/inbox/runs") return { data: runs, isLoading: false, error: null, mutate: vi.fn() };
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

describe("useInboxTriage", () => {
  it("returns pending approvals + failed (non-acked) runs and a count", () => {
    mockRead(
      { approvals: [
        { id: "a1", title: "Deploy?", status: "pending" },
        { id: "a2", title: "old", status: "approved" }, // filtered out
      ] },
      { runs: [
        { id: "r1", agentId: "ag1", status: "failed" },
        { id: "r2", agentId: "ag1", status: "succeeded" }, // filtered out
        { id: "r3", agentId: "ag1", status: "failed" },   // acked below
      ] },
    );
    const { result } = renderHook(() => useInboxTriage(new Set(["r3"])));
    expect(result.current.approvals.map((a) => a.id)).toEqual(["a1"]);
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(2);
  });

  it("normalizes bare-array and {items} run payloads and tolerates undefined", () => {
    mockRead(undefined, [{ id: "r1", status: "failed" }]);
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.approvals).toEqual([]);
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(1);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- useInboxTriage`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the hook**

```ts
// apps/frontend/src/components/teams/inbox/hooks/useInboxTriage.ts
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Approval, HeartbeatRun } from "@/components/teams/shared/types";

export interface UseInboxTriageResult {
  approvals: Approval[];
  failedRuns: HeartbeatRun[];
  count: number;
  isLoading: boolean;
}

type ApprovalsResponse = { approvals?: Approval[] } | Approval[] | undefined;
type RunsResponse =
  | { runs?: HeartbeatRun[] }
  | { items?: HeartbeatRun[] }
  | HeartbeatRun[]
  | undefined;

function asApprovals(data: ApprovalsResponse): Approval[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.approvals ?? [];
}

function asRuns(data: RunsResponse): HeartbeatRun[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return (data as { runs?: HeartbeatRun[] }).runs ?? (data as { items?: HeartbeatRun[] }).items ?? [];
}

/**
 * Merge the two triage sources for the "Needs attention" band.
 *
 * - Approvals: the real list endpoint `/approvals` (the `/inbox/approvals`
 *   query key is an empty tab stub — do NOT use it). Filtered to pending.
 * - Failed runs: `/inbox/runs` (BFF already forwards status=failed; we
 *   re-filter defensively) minus the locally-acknowledged ids.
 *
 * `acknowledgedRunIds` is owned by the caller (NeedsAttentionBand via
 * useAcknowledgedRuns) so the dismiss state is shared with the rows.
 */
export function useInboxTriage(acknowledgedRunIds: Set<string>): UseInboxTriageResult {
  const { read } = useTeamsApi();
  const approvalsRes = read<ApprovalsResponse>("/approvals");
  const runsRes = read<RunsResponse>("/inbox/runs");

  const approvals = asApprovals(approvalsRes.data).filter((a) => a.status === "pending");
  const failedRuns = asRuns(runsRes.data).filter(
    (r) => r.status === "failed" && !acknowledgedRunIds.has(r.id),
  );

  return {
    approvals,
    failedRuns,
    count: approvals.length + failedRuns.length,
    isLoading: approvalsRes.isLoading || runsRes.isLoading,
  };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- useInboxTriage`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/hooks/useInboxTriage.ts src/__tests__/teams/useInboxTriage.test.tsx
git commit -m "feat(teams): useInboxTriage hook merging approvals + failed runs"
```

---

## Task 4: useApprovalMutations hook

**Files:**
- Create: `src/components/teams/inbox/hooks/useApprovalMutations.ts`
- Test: `src/__tests__/teams/useApprovalMutations.test.tsx`

`approve(id)` / `reject(id, reason)` post to the existing BFF, then revalidate `/teams/approvals` (the band's source) + `/teams/dashboard` (the pending-approvals tile). Mirrors `useAgentMutations`' use of `useSWRConfig().mutate`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/useApprovalMutations.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const post = vi.fn();
const swrMutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";

beforeEach(() => { post.mockReset().mockResolvedValue({}); swrMutate.mockReset(); });

describe("useApprovalMutations", () => {
  it("approve posts {} and revalidates approvals + dashboard", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await act(async () => { await result.current.approve("a1"); });
    expect(post).toHaveBeenCalledWith("/approvals/a1/approve", {});
    expect(swrMutate).toHaveBeenCalledWith("/teams/approvals");
    expect(swrMutate).toHaveBeenCalledWith("/teams/dashboard");
  });

  it("reject posts {reason} and revalidates", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await act(async () => { await result.current.reject("a1", "no good"); });
    expect(post).toHaveBeenCalledWith("/approvals/a1/reject", { reason: "no good" });
    expect(swrMutate).toHaveBeenCalledWith("/teams/approvals");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- useApprovalMutations`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the hook**

```ts
// apps/frontend/src/components/teams/inbox/hooks/useApprovalMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const APPROVALS_KEY = "/teams/approvals";
const DASHBOARD_KEY = "/teams/dashboard"; // pending-approvals tile

/**
 * Approve / reject a pending approval. Both bodies are whitelisted by the BFF
 * (extra="forbid"): approve accepts an optional {note} (not surfaced in v1 —
 * one-click approve), reject requires {reason}. On success we revalidate the
 * band's approvals source + the dashboard tile; the approved/rejected item
 * leaves the pending list on refetch.
 */
export function useApprovalMutations() {
  const { post } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(() => {
    mutate(APPROVALS_KEY);
    mutate(DASHBOARD_KEY);
  }, [mutate]);

  const approve = useCallback(
    async (approvalId: string) => {
      await post(`/approvals/${approvalId}/approve`, {});
      revalidate();
    },
    [post, revalidate],
  );

  const reject = useCallback(
    async (approvalId: string, reason: string) => {
      await post(`/approvals/${approvalId}/reject`, { reason });
      revalidate();
    },
    [post, revalidate],
  );

  return { approve, reject };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- useApprovalMutations`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/hooks/useApprovalMutations.ts src/__tests__/teams/useApprovalMutations.test.tsx
git commit -m "feat(teams): useApprovalMutations hook (approve/reject + revalidate)"
```

---

## Task 5: ApprovalRow component

**Files:**
- Create: `src/components/teams/inbox/ApprovalRow.tsx`
- Test: `src/__tests__/teams/ApprovalRow.test.tsx`

One approval. **Approve** = one click. **Reject** = expands an inline required-reason field (`EditorialInput`); the confirm button is disabled until the reason is non-empty. Busy state disables buttons during the async call; an inline error message shows on failure (row retained).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/ApprovalRow.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

const A: Approval = { id: "a1", title: "Deploy to prod?", status: "pending", createdAt: "2026-05-29T00:00:00Z" };

beforeEach(() => { approve.mockReset().mockResolvedValue(undefined); reject.mockReset().mockResolvedValue(undefined); });

describe("ApprovalRow", () => {
  it("Approve calls approve(id)", async () => {
    render(<ApprovalRow approval={A} />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a1"));
  });

  it("Reject requires a reason: confirm disabled until typed, then posts reason", async () => {
    render(<ApprovalRow approval={A} />);
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));
    const confirm = screen.getByRole("button", { name: /confirm reject/i });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText(/reason/i), { target: { value: "nope" } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(reject).toHaveBeenCalledWith("a1", "nope"));
  });

  it("falls back to a default title when none is given", () => {
    render(<ApprovalRow approval={{ id: "a2", status: "pending" }} />);
    expect(screen.getByText(/approval requested/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- ApprovalRow`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/ApprovalRow.tsx
"use client";

import { useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { EditorialButton, EditorialInput } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import type { Approval } from "@/components/teams/shared/types";

export function ApprovalRow({ approval }: { approval: Approval }) {
  const { approve, reject } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const title = approval.title?.trim() ? approval.title : "Approval requested";

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-col gap-2 px-3 py-2">
      <div className="flex items-center gap-2">
        <ShieldQuestion className="h-4 w-4 flex-shrink-0" style={{ color: "var(--forest)" }} />
        <span className="flex-1 truncate text-[var(--ink)]">{title}</span>
        {approval.createdAt && (
          <span className="text-xs text-[var(--mute)]">{timeAgo(approval.createdAt)}</span>
        )}
        {!rejecting && (
          <div className="flex gap-2">
            <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
              Approve
            </EditorialButton>
            <EditorialButton variant="destructive" size="sm" disabled={busy} onClick={() => setRejecting(true)}>
              Reject
            </EditorialButton>
          </div>
        )}
      </div>
      {rejecting && (
        <div className="flex items-center gap-2">
          <EditorialInput
            autoFocus
            placeholder="Reason for rejection"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            className="flex-1"
          />
          <EditorialButton
            variant="destructive"
            size="sm"
            disabled={busy || reason.trim() === ""}
            aria-label="Confirm reject"
            onClick={() => run(async () => { await reject(approval.id, reason.trim()); })}
          >
            Confirm reject
          </EditorialButton>
          <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => { setRejecting(false); setReason(""); }}>
            Cancel
          </EditorialButton>
        </div>
      )}
      {error && <span className="text-xs text-[var(--danger)]">{error}</span>}
    </li>
  );
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- ApprovalRow`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/ApprovalRow.tsx src/__tests__/teams/ApprovalRow.test.tsx
git commit -m "feat(teams): ApprovalRow with one-click approve + inline reject reason"
```

---

## Task 6: FailedRunRow component

**Files:**
- Create: `src/components/teams/inbox/FailedRunRow.tsx`
- Test: `src/__tests__/teams/FailedRunRow.test.tsx`

One failed run. **View** → `/teams/agents/{agentId}/runs/{id}` (a plain `<a href>`; hidden when `agentId` is null). **Dismiss** → calls `onDismiss()` (the band wires this to `useAcknowledgedRuns.ack`).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/FailedRunRow.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import type { HeartbeatRun } from "@/components/teams/shared/types";

const RUN: HeartbeatRun = { id: "r1", agentId: "ag1", status: "failed", failureReason: "boom", startedAt: "2026-05-29T00:00:00Z" };

describe("FailedRunRow", () => {
  it("renders agent name + failure reason and View links to the run detail", () => {
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} />);
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view/i })).toHaveAttribute(
      "href",
      "/teams/agents/ag1/runs/r1",
    );
  });

  it("hides View when agentId is null", () => {
    render(<FailedRunRow run={{ id: "r2", agentId: null, status: "failed" }} agentName="?" onDismiss={vi.fn()} />);
    expect(screen.queryByRole("link", { name: /view/i })).toBeNull();
  });

  it("Dismiss calls onDismiss", () => {
    const onDismiss = vi.fn();
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- FailedRunRow`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/FailedRunRow.tsx
"use client";

import { AlertTriangle } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { HeartbeatRun } from "@/components/teams/shared/types";

export function FailedRunRow({
  run,
  agentName,
  onDismiss,
}: {
  run: HeartbeatRun;
  agentName: string;
  onDismiss: () => void;
}) {
  return (
    <li className="flex items-center gap-2 px-3 py-2">
      <AlertTriangle className="h-4 w-4 flex-shrink-0" style={{ color: "var(--danger)" }} />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[var(--ink)]">
          {agentName} — heartbeat run failed
        </span>
        {run.failureReason && (
          <span className="truncate text-xs text-[var(--mute)]">{run.failureReason}</span>
        )}
      </div>
      {run.startedAt && <span className="text-xs text-[var(--mute)]">{timeAgo(run.startedAt)}</span>}
      <div className="flex gap-2">
        {run.agentId && (
          <a
            href={`/teams/agents/${run.agentId}/runs/${run.id}`}
            className="text-sm hover:underline"
            style={{ color: "var(--forest)" }}
          >
            View
          </a>
        )}
        <EditorialButton variant="secondary" size="sm" onClick={onDismiss}>
          Dismiss
        </EditorialButton>
      </div>
    </li>
  );
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- FailedRunRow`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/FailedRunRow.tsx src/__tests__/teams/FailedRunRow.test.tsx
git commit -m "feat(teams): FailedRunRow with View link + local dismiss"
```

---

## Task 7: NeedsAttentionBand component

**Files:**
- Create: `src/components/teams/inbox/NeedsAttentionBand.tsx`
- Test: `src/__tests__/teams/NeedsAttentionBand.test.tsx`

Composition root: owns `useAcknowledgedRuns(companyId)` (passes the set to `useInboxTriage`, and `ack` to each `FailedRunRow`), owns collapse state (lazy `loadBandCollapsed` + `saveBandCollapsed` on toggle), renders **nothing when count is 0**, shows the count header, and renders approval rows before failed-run rows. Resolves each run's agent name from the `agents` prop.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/NeedsAttentionBand.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const triage = { approvals: [] as unknown[], failedRuns: [] as unknown[], count: 0, isLoading: false };
vi.mock("@/components/teams/inbox/hooks/useInboxTriage", () => ({
  useInboxTriage: () => triage,
}));
// Real useAcknowledgedRuns + storage are fine (jsdom localStorage). Stub the
// mutations the rows pull in so no network is touched.
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve: vi.fn(), reject: vi.fn() }),
}));

import { NeedsAttentionBand } from "@/components/teams/inbox/NeedsAttentionBand";

beforeEach(() => {
  localStorage.clear();
  triage.approvals = [];
  triage.failedRuns = [];
  triage.count = 0;
});

describe("NeedsAttentionBand", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<NeedsAttentionBand companyId="co1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows count + both row types; collapse hides the rows", () => {
    triage.approvals = [{ id: "a1", title: "Deploy?", status: "pending" }];
    triage.failedRuns = [{ id: "r1", agentId: "ag1", status: "failed" }];
    triage.count = 2;
    render(
      <NeedsAttentionBand
        companyId="co1"
        agents={[{ id: "ag1", name: "Atlas" }]}
      />,
    );
    expect(screen.getByText(/needs attention/i)).toBeInTheDocument();
    expect(screen.getByText(/Deploy\?/)).toBeInTheDocument();
    expect(screen.getByText(/Atlas — heartbeat run failed/)).toBeInTheDocument();
    // Collapse toggle hides the rows.
    fireEvent.click(screen.getByRole("button", { name: /needs attention/i }));
    expect(screen.queryByText(/Deploy\?/)).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- NeedsAttentionBand`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/NeedsAttentionBand.tsx
"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { loadBandCollapsed, saveBandCollapsed } from "@/components/teams/shared/lib/inboxStorage";
import type { CompanyAgent } from "@/components/teams/shared/types";

export function NeedsAttentionBand({
  companyId,
  agents,
}: {
  companyId: string;
  agents: CompanyAgent[];
}) {
  const { acknowledgedRunIds, ack } = useAcknowledgedRuns(companyId);
  const { approvals, failedRuns, count } = useInboxTriage(acknowledgedRunIds);
  const [collapsed, setCollapsed] = useState<boolean>(() => loadBandCollapsed(companyId));

  // Nothing to triage — render nothing so "inbox zero" stays clean.
  if (count === 0) return null;

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    saveBandCollapsed(companyId, next);
  }

  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");

  return (
    <section
      className="mb-4 rounded-md border"
      style={{ borderColor: "var(--edge)", background: "var(--surface)" }}
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        <span className="font-medium text-[var(--ink)]">Needs attention</span>
        <span className="text-xs text-[var(--mute)]">{count}</span>
      </button>
      {!collapsed && (
        <ul className="divide-y" style={{ borderColor: "var(--edge)" }}>
          {approvals.map((a) => (
            <ApprovalRow key={a.id} approval={a} />
          ))}
          {failedRuns.map((r) => (
            <FailedRunRow
              key={r.id}
              run={r}
              agentName={agentName(r.agentId)}
              onDismiss={() => ack(r.id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- NeedsAttentionBand`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/NeedsAttentionBand.tsx src/__tests__/teams/NeedsAttentionBand.test.tsx
git commit -m "feat(teams): NeedsAttentionBand assembling triage rows"
```

---

## Task 8: Realtime keys for approvals + failed runs

**Files:**
- Modify: `src/components/teams/TeamsEventsProvider.tsx`
- Test: `src/__tests__/teams/TeamsEventsProvider.test.tsx` (extend)

The band reads `/teams/approvals` + `/teams/inbox/runs`. Wire those keys into the event map so they refetch live. Export `EVENT_KEY_MAP` so the mapping is directly testable.

- [ ] **Step 1: Write the failing test** (append to the existing test file)

```tsx
import { EVENT_KEY_MAP } from "@/components/teams/TeamsEventsProvider";

describe("EVENT_KEY_MAP triage wiring", () => {
  it("activity.logged invalidates approvals", () => {
    expect(EVENT_KEY_MAP["teams.activity.logged"]).toContain("/teams/approvals");
  });
  it("heartbeat run events invalidate the failed-runs source", () => {
    expect(EVENT_KEY_MAP["teams.heartbeat.run.queued"]).toContain("/teams/inbox/runs");
    expect(EVENT_KEY_MAP["teams.heartbeat.run.status"]).toContain("/teams/inbox/runs");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- TeamsEventsProvider`
Expected: FAIL — `EVENT_KEY_MAP` is not exported / keys missing.

- [ ] **Step 3: Implement** — change the `EVENT_KEY_MAP` declaration to be exported and add the keys:

```tsx
export const EVENT_KEY_MAP: Record<string, string[]> = {
  "teams.activity.logged": [
    "/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues",
    "/teams/approvals",
  ],
  "teams.agent.status": ["/teams/dashboard", "/teams/agents"],
  "teams.heartbeat.run.queued": ["/teams/dashboard", "/teams/inbox", "/teams/inbox/runs"],
  "teams.heartbeat.run.status": ["/teams/dashboard", "/teams/inbox", "/teams/inbox/runs"],
  "teams.heartbeat.run.event": [],
  "teams.heartbeat.run.log": [],
};
```

(`mutateKeyPrefix` already matches a bare key exactly, so `/teams/approvals` and `/teams/inbox/runs` — which carry no query string — are invalidated correctly.)

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- TeamsEventsProvider`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/TeamsEventsProvider.tsx src/__tests__/teams/TeamsEventsProvider.test.tsx
git commit -m "feat(teams): live-invalidate approvals + failed-runs for the triage band"
```

---

## Task 9: Mount the band in InboxPage

**Files:**
- Modify: `src/components/teams/inbox/InboxPage.tsx`
- Test: `src/__tests__/components/teams/inbox/InboxPage.test.tsx` (keep green)

- [ ] **Step 1: Add the import** (with the other inbox component imports near the top)

```tsx
import { NeedsAttentionBand } from "@/components/teams/inbox/NeedsAttentionBand";
```

- [ ] **Step 2: Render the band** — inside the returned JSX, immediately after `<PanelMasthead .../>` and before `<InboxToolbar ...>`:

```tsx
      <PanelMasthead overline="Inbox" title="Inbox" className="mb-4" />
      <NeedsAttentionBand companyId={companyId} agents={agents} />
      <InboxToolbar
```

- [ ] **Step 3: Keep the existing InboxPage test green** — the band now calls `useTeamsApi().read("/approvals")` + `read("/inbox/runs")`. Open `src/__tests__/components/teams/inbox/InboxPage.test.tsx`; in its `useTeamsApi` mock's `read`, ensure unknown paths (including `/approvals` and `/inbox/runs`) return `{ data: undefined, isLoading: false, error: null, mutate: vi.fn() }` (a default branch). With no triage data the band renders null, so existing assertions are unaffected. If the mock already has a catch-all returning that shape, no change is needed.

- [ ] **Step 4: Run the tests**

Run: `pnpm test -- InboxPage`
Expected: PASS (band renders null on empty mocks; existing assertions unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/inbox/InboxPage.tsx "src/__tests__/components/teams/inbox/InboxPage.test.tsx"
git commit -m "feat(teams): mount NeedsAttentionBand above the inbox toolbar"
```

---

## Task 10: Retire the standalone ApprovalsPanel

**Files:**
- Modify: `src/components/teams/TeamsSidebar.tsx`
- Modify: `src/components/teams/TeamsPanelRouter.tsx`
- Delete: `src/components/teams/panels/ApprovalsPanel.tsx`
- Delete: `src/__tests__/teams/ApprovalsPanel.test.tsx`
- Test: `src/__tests__/teams/TeamsPanelRouter.redirect.test.tsx` (new)

- [ ] **Step 1: Write the failing test** (router redirects the retired `approvals` key)

```tsx
// apps/frontend/src/__tests__/teams/TeamsPanelRouter.redirect.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

describe("TeamsPanelRouter retirement", () => {
  it("redirects the retired approvals panel to the inbox", () => {
    render(<TeamsPanelRouter panel="approvals" />);
    expect(replace).toHaveBeenCalledWith("/teams/inbox");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- TeamsPanelRouter.redirect`
Expected: FAIL — `approvals` still maps to ApprovalsPanel (no redirect).

- [ ] **Step 3: Remove the sidebar entry** — in `TeamsSidebar.tsx`, delete the line:

```tsx
  { key: "approvals", label: "Approvals", Icon: ClipboardCheck },
```

If `ClipboardCheck` is now unused, remove it from the `lucide-react` import to satisfy eslint.

- [ ] **Step 4: Update the router** — in `TeamsPanelRouter.tsx`: remove the `approvals` entry from `PANELS`, drop the `ApprovalsPanel` dynamic import, and redirect retired keys. Replace the render function:

```tsx
"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// ... existing PANELS object WITHOUT the `approvals` entry ...

// Panels removed from the nav but still reachable by old URLs/bookmarks.
const RETIRED_REDIRECTS: Record<string, string> = {
  approvals: "/teams/inbox", // folded into the inbox "Needs attention" band
};

export function TeamsPanelRouter({ panel }: { panel: string }) {
  const router = useRouter();
  const redirectTo = RETIRED_REDIRECTS[panel];

  useEffect(() => {
    if (redirectTo) router.replace(redirectTo);
  }, [redirectTo, router]);

  if (redirectTo) return null;

  const Cmp = PANELS[panel];
  if (!Cmp) return <div className="p-8">Unknown panel: {panel}</div>;
  return <Cmp />;
}
```

- [ ] **Step 5: Delete the panel + its test**

```bash
git rm src/components/teams/panels/ApprovalsPanel.tsx src/__tests__/teams/ApprovalsPanel.test.tsx
```

- [ ] **Step 6: Run tests + lint the touched files**

Run: `pnpm test -- TeamsPanelRouter.redirect`
Expected: PASS.
Run: `pnpm exec eslint src/components/teams/TeamsSidebar.tsx src/components/teams/TeamsPanelRouter.tsx`
Expected: clean (no unused `ClipboardCheck` / `ApprovalsPanel`).

- [ ] **Step 7: Commit**

```bash
git add src/components/teams/TeamsSidebar.tsx src/components/teams/TeamsPanelRouter.tsx src/__tests__/teams/TeamsPanelRouter.redirect.test.tsx
git commit -m "feat(teams): retire standalone Approvals panel (folded into inbox band)"
```

---

## Task 11: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Typecheck**

Run: `pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 2: Lint the new/changed files**

Run: `pnpm exec eslint src/components/teams/inbox src/components/teams/TeamsEventsProvider.tsx src/components/teams/TeamsSidebar.tsx src/components/teams/TeamsPanelRouter.tsx src/components/teams/shared/lib/inboxStorage.ts`
Expected: clean.

- [ ] **Step 3: Run the full frontend test suite**

Run: `pnpm test`
Expected: all triage + existing teams tests pass. (Pre-existing local-env-only failures unrelated to this change — `CreditsPanel`, `ChatGPTOAuthStep`, `useChatGPTOAuth`, `BotSetupWizard` — may fail locally without env/keys; they pass in CI. Confirm none of the failures are in files this plan touched.)

- [ ] **Step 4: Production build (catches App Router / "use client" issues vitest misses)**

Run: `pnpm run build`
Expected: build succeeds.

- [ ] **Step 5: Update the roadmap row E** — in `docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md`, set sub-project **E**'s Status to `In progress` (PR open) and link the spec + this plan. Commit:

```bash
git add docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md
git commit -m "docs(teams): mark roadmap E (inbox mixed triage) in progress"
```

---

## Self-review (against the spec)

**Spec coverage:**
- Needs-attention band (collapsible, count, hide-when-empty, approvals-first) → Task 7.
- Approval row (one-click approve; inline required reason) → Task 5. (Spec's "optional note" on approve is API-supported but intentionally not surfaced — one-click approve; the inline field is reject-only, exactly as the spec specifies.)
- Failed-run row (View link to `/teams/agents/{agentId}/runs/{id}`, hidden when no agentId; local Dismiss) → Task 6.
- Local per-user acknowledge → Tasks 1 + 2; wired in Task 7.
- Data sources `/approvals` + `/inbox/runs`, no new BFF → Task 3.
- Realtime keys → Task 8.
- ApprovalsPanel retirement + sidebar + `/teams/approvals` redirect → Task 10.
- Approvals-first ordering → Task 7 (approvals mapped before failedRuns).
- Joins excluded → not implemented anywhere (correct).

**Type consistency:** `Approval`/`HeartbeatRun`/`CompanyAgent` from `shared/types.ts` used uniformly; `useInboxTriage(acknowledgedRunIds: Set<string>)` matches the band's call; `useApprovalMutations().approve(id)`/`reject(id, reason)` match `ApprovalRow`; `FailedRunRow({run, agentName, onDismiss})` matches the band; SWR keys `/teams/approvals` + `/teams/inbox/runs` consistent across the hook, mutations, and realtime map.

**Placeholder scan:** none — every code step is complete.
