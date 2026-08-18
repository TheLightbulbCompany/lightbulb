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
