import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGetStarted } from "../useGetStarted";
import { emitStep } from "../getStartedEvents";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn(), captureException: vi.fn() }));

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
    // Re-apply the implementation each run: the global afterEach calls
    // vi.restoreAllMocks(), which strips mockResolvedValue and would otherwise
    // make update() return undefined (the persistence effect calls .catch on it).
    update.mockReset().mockResolvedValue(undefined);
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

  it("open() clears and re-persists the dismissed flag (hook contract)", () => {
    const { result } = renderHook(() => useGetStarted({ derived: {} }));

    // Dismiss: card closes and the dismissed flag is set + persisted.
    act(() => { result.current.dismiss(); });
    expect(result.current.isOpen).toBe(false);
    expect(result.current.dismissed).toBe(true);
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ unsafeMetadata: expect.objectContaining({ getStarted: expect.objectContaining({ dismissed: true }) }) }),
    );

    // open() clears (and persists) the dismissed flag. This is the hook-level
    // contract behind the active-guide launcher's reopen affordance (used when
    // the card was minimized, not dismissed). A permanent dismiss unmounts the
    // GetStarted component, so no dismissed-state UI path calls open() — but the
    // hook contract itself still holds.
    act(() => { result.current.open(); });
    expect(result.current.isOpen).toBe(true);
    expect(result.current.dismissed).toBe(false);
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ unsafeMetadata: expect.objectContaining({ getStarted: expect.objectContaining({ dismissed: false }) }) }),
    );
  });

  it("does NOT clobber: reading the final article + completion in one tick persists BOTH read and completedFired", async () => {
    // A returning user (seen:true → no auto-open) who already has 4/5 steps
    // satisfied via live derived signals. Reading the last article ("meet")
    // completes step 5, which fires the completion effect — two state changes
    // (read append + completedFired) cascade in the same interaction. Under the
    // old partial-patch persist these raced on Clerk's stale base and the
    // completion write dropped `read` (or vice-versa). With whole-object writes
    // from local truth, every write is complete, so neither key is lost.
    clerkState = {
      isLoaded: true,
      user: { unsafeMetadata: { getStarted: { seen: true, read: [], done: [] } }, update },
    };
    const { result } = renderHook(() =>
      useGetStarted({ derived: { chat: true, task: true, hire: true, channel: true } }),
    );
    expect(result.current.progress).toEqual({ done: 4, total: 5 });

    await act(async () => { result.current.openArticle("meet-friday-atlas"); });

    expect(result.current.progress).toEqual({ done: 5, total: 5 });

    // The write that carries the completion flag must ALSO carry the read
    // article — i.e. completion did not clobber the read append.
    const writes = update.mock.calls.map(
      (c) => (c[0] as { unsafeMetadata: { getStarted: Record<string, unknown> } }).unsafeMetadata.getStarted,
    );
    const completionWrite = writes.find((g) => g.completedFired === true);
    expect(completionWrite).toBeDefined();
    expect(completionWrite).toEqual(
      expect.objectContaining({ read: ["meet-friday-atlas"], completedFired: true }),
    );

    // And the final persisted snapshot (last write wins on Clerk) has both keys.
    const last = writes[writes.length - 1];
    expect(last).toEqual(
      expect.objectContaining({ read: ["meet-friday-atlas"], completedFired: true }),
    );
  });
});
