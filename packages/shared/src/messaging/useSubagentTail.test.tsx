// @vitest-environment jsdom
import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useSubagentTail } from "./useSubagentTail";

describe("useSubagentTail", () => {
  it("polls every 2 s while a target is set and stops when cleared", async () => {
    vi.useFakeTimers();
    const subagentTail = vi.fn().mockResolvedValue({ subagentId: "sa-1", available: true, text: "hi", truncated: false });
    const client = { subagentTail } as never;
    const { result, rerender } = renderHook(({ t }) => useSubagentTail(client, t), {
      initialProps: { t: { roomId: "r", agentId: "a", subagentId: "sa-1" } as { roomId: string; agentId: string; subagentId: string } | null },
    });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.tail?.text).toBe("hi");
    await act(() => vi.advanceTimersByTimeAsync(4_000));
    expect(subagentTail).toHaveBeenCalledTimes(3);
    rerender({ t: null });
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(subagentTail).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });
});
