// @vitest-environment jsdom
import { renderHook, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSubagentTail } from "./useSubagentTail";

describe("useSubagentTail", () => {
  afterEach(() => { vi.useRealTimers(); });

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
  });

  it("clears the previous target's text when the target changes", async () => {
    vi.useFakeTimers();
    const subagentTail = vi.fn()
      .mockResolvedValueOnce({ subagentId: "sa-1", available: true, text: "first", truncated: false })
      .mockReturnValue(new Promise(() => {}));
    const client = { subagentTail } as never;
    const { result, rerender } = renderHook(({ id }) => useSubagentTail(client, { roomId: "r", agentId: "a", subagentId: id }), { initialProps: { id: "sa-1" } });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.tail?.text).toBe("first");
    rerender({ id: "sa-2" });
    expect(result.current.tail).toBeNull();
  });
});
