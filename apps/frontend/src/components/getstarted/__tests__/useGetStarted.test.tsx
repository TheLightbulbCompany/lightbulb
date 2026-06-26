import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGetStarted } from "../useGetStarted";
import { emitStep } from "../getStartedEvents";

const update = vi.fn().mockResolvedValue(undefined);
let meta: Record<string, unknown> = {};
vi.mock("@clerk/nextjs", () => ({
  useUser: (): { isLoaded: boolean; user: { unsafeMetadata: Record<string, unknown>; update: typeof update } } => ({
    isLoaded: true,
    user: { unsafeMetadata: meta, update },
  }),
}));

describe("useGetStarted", () => {
  beforeEach(() => { meta = {}; update.mockClear(); });

  it("marks steps done from derived live signals (channel + task)", () => {
    const { result } = renderHook(() => useGetStarted({ derived: { channel: true, task: true } }));
    expect(result.current.completed.channel).toBe(true);
    expect(result.current.completed.task).toBe(true);
    expect(result.current.progress).toEqual({ done: 2, total: 5 });
  });

  it("completes an action step when its event fires and persists it", async () => {
    const { result } = renderHook(() => useGetStarted({ derived: {} }));
    await act(async () => { emitStep("hire"); });
    expect(result.current.completed.hire).toBe(true);
    expect(update).toHaveBeenCalled();
  });

  it("opens automatically on first run and records seen", async () => {
    const { result } = renderHook(() => useGetStarted({ derived: {} }));
    expect(result.current.isOpen).toBe(true);
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ unsafeMetadata: expect.objectContaining({ getStarted: expect.objectContaining({ seen: true }) }) }),
    );
  });
});
