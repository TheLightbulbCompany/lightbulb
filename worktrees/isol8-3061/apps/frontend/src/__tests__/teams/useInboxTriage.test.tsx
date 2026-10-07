// apps/frontend/src/__tests__/teams/useInboxTriage.test.tsx
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";

function mockRead(runs: unknown) {
  read.mockImplementation((path: string) => {
    if (path === "/inbox/runs") return { data: runs, isLoading: false, error: null, mutate: vi.fn() };
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

describe("useInboxTriage", () => {
  it("returns failed (non-acked) runs and a count", () => {
    mockRead(
      { runs: [
        { id: "r1", agentId: "ag1", status: "failed" },
        { id: "r2", agentId: "ag1", status: "succeeded" }, // filtered out
        { id: "r3", agentId: "ag1", status: "failed" },   // acked below
      ] },
    );
    const { result } = renderHook(() => useInboxTriage(new Set(["r3"])));
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(1);
  });

  it("does NOT read approvals — decisions live on the attention feed", () => {
    // Approvals moved to the "Needs you" tab so a decision has exactly one
    // home and one count. If this band starts reading /approvals again, the
    // same approval renders twice under two different totals.
    mockRead({ runs: [] });
    renderHook(() => useInboxTriage(new Set()));
    expect(read).not.toHaveBeenCalledWith("/approvals");
  });

  it("normalizes bare-array run payloads and tolerates undefined", () => {
    mockRead([{ id: "r1", status: "failed" }]);
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(1);
  });

  it("normalizes the {items} run-payload wrapper", () => {
    mockRead({ items: [{ id: "r9", agentId: "ag1", status: "failed" }] });
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r9"]);
    expect(result.current.count).toBe(1);
  });
});
