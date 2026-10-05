// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

const messaging = vi.hoisted(() => ({ searchMessages: vi.fn(), warmSearch: vi.fn() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging }) }));

import { useMessageSearch } from "../useMessageSearch";

const hit = { messageId: "m1", roomId: "r1", messageSeq: 1, threadRootMessageId: null, authorName: "Ann",
  authorKind: "human", createdAt: "2026-10-04T00:00:00Z", snippet: "the \u0001deploy\u0002 fix" };

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("useMessageSearch", () => {
  it("warms once when enabled, then searches after a pause", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockResolvedValue({ hits: [hit] });
    const { result, rerender } = renderHook(({ q }) => useMessageSearch(q, { enabled: true, limit: 8, minLength: 2 }), { initialProps: { q: "d" } });
    expect(messaging.warmSearch).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe("idle");
    rerender({ q: "deploy" });
    expect(result.current.state).toBe("loading");
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(messaging.searchMessages).toHaveBeenCalledWith("deploy", { roomId: undefined, limit: 8 });
    expect(result.current).toEqual({ hits: [hit], state: "done" });
  });

  it("reports warming on a 503 and retries once", async () => {
    vi.useFakeTimers();
    messaging.warmSearch.mockResolvedValue(undefined);
    messaging.searchMessages.mockRejectedValueOnce({ status: 503 }).mockResolvedValueOnce({ hits: [hit] });
    const { result } = renderHook(() => useMessageSearch("deploy", { enabled: true, roomId: "r1", limit: 50 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(result.current.state).toBe("warming");
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(messaging.searchMessages).toHaveBeenCalledTimes(2);
    expect(result.current.state).toBe("done");
  });
});
