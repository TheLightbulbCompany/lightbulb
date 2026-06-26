import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGetStarted } from "../useGetStarted";
import { emitStep } from "../getStartedEvents";

const update = vi.fn().mockResolvedValue(undefined);
type ClerkUser = { unsafeMetadata: Record<string, unknown>; update: typeof update } | null;
// Mutable so a test can drive Clerk's async load: `isLoaded`/`user` change
// across renders (hard reload first returns `{ isLoaded: false, user: null }`).
let clerkState: { isLoaded: boolean; user: ClerkUser } = {
  isLoaded: true,
  user: { unsafeMetadata: {}, update },
};
vi.mock("@clerk/nextjs", () => ({
  useUser: (): { isLoaded: boolean; user: ClerkUser } => clerkState,
}));

describe("useGetStarted", () => {
  beforeEach(() => {
    clerkState = { isLoaded: true, user: { unsafeMetadata: {}, update } };
    update.mockClear();
  });

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

  it("re-hydrates a returning user once Clerk loads asynchronously (no auto-open, completion restored)", () => {
    // Hard reload: Clerk first reports not-loaded with no user.
    clerkState = { isLoaded: false, user: null };
    const { result, rerender } = renderHook(() => useGetStarted({ derived: {} }));
    expect(result.current.isOpen).toBe(false);
    expect(result.current.completed.chat).toBe(false);
    expect(update).not.toHaveBeenCalled();

    // Clerk finishes loading a returning user who has seen the guide and
    // already completed "chat".
    clerkState = {
      isLoaded: true,
      user: { unsafeMetadata: { getStarted: { seen: true, done: ["chat"] } }, update },
    };
    rerender();

    expect(result.current.isOpen).toBe(false); // does NOT auto-open for a returning user
    expect(result.current.completed.chat).toBe(true); // completion re-hydrated from metadata
  });
});
