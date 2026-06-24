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
  it("returns actionable approvals (pending + revision_requested) + failed (non-acked) runs and a count", () => {
    mockRead(
      { approvals: [
        { id: "a1", type: "hire_agent", status: "pending" },
        { id: "a2", type: "hire_agent", status: "approved" }, // terminal — filtered out
        { id: "a4", type: "hire_agent", status: "revision_requested" }, // actionable — kept
      ] },
      { runs: [
        { id: "r1", agentId: "ag1", status: "failed" },
        { id: "r2", agentId: "ag1", status: "succeeded" }, // filtered out
        { id: "r3", agentId: "ag1", status: "failed" },   // acked below
      ] },
    );
    const { result } = renderHook(() => useInboxTriage(new Set(["r3"])));
    expect(result.current.approvals.map((a) => a.id)).toEqual(["a1", "a4"]);
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(3);
  });

  it("normalizes bare-array run payloads and tolerates undefined", () => {
    mockRead(undefined, [{ id: "r1", status: "failed" }]);
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.approvals).toEqual([]);
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(1);
  });

  it("normalizes the {items} run-payload wrapper", () => {
    mockRead({ approvals: [] }, { items: [{ id: "r9", agentId: "ag1", status: "failed" }] });
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r9"]);
    expect(result.current.count).toBe(1);
  });
});
