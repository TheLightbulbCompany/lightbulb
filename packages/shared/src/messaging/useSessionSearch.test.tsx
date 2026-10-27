// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionSearch } from "./useSessionSearch";

const hit = { sessionId: "s1", seq: 4, authorKind: "person" as const, authorId: "u1", authorName: "", createdAt: "2026-10-26T00:00:00Z",
  snippet: "\u0001deploy\u0002", sessionTitle: "Fix", provider: "claude", humanUserId: "u1" };

describe("useSessionSearch", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("warms, debounces and retries once while search wakes", async () => {
    const searchSessions = vi.fn()
      .mockRejectedValueOnce({ status: 503 })
      .mockResolvedValueOnce({ hits: [hit] });
    const client = { searchSessions, warmSearch: vi.fn().mockResolvedValue(undefined) };
    const { result } = renderHook(() => useSessionSearch(client, "deploy", { enabled: true, limit: 50 }));
    expect(client.warmSearch).toHaveBeenCalled();
    expect(result.current.state).toBe("loading");
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(result.current.state).toBe("warming");
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(searchSessions).toHaveBeenCalledTimes(2);
    expect(searchSessions).toHaveBeenLastCalledWith("deploy", { limit: 50 });
    expect(result.current).toEqual({ hits: [hit], state: "done" });
  });

  it("does not search one letter", () => {
    const client = { searchSessions: vi.fn(), warmSearch: vi.fn().mockResolvedValue(undefined) };
    const { result } = renderHook(() => useSessionSearch(client, "d", { enabled: true }));
    vi.advanceTimersByTime(1_000);
    expect(result.current.state).toBe("idle");
    expect(client.searchSessions).not.toHaveBeenCalled();
  });
});
