import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  requestFocusComposer,
  consumeFocusComposerRequest,
  deferFocusComposerRequest,
  releaseFocusComposerRequest,
  _resetFocusComposerRequestForTests,
} from "../focusComposerRequest";

beforeEach(() => {
  _resetFocusComposerRequestForTests();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("focusComposerRequest", () => {
  it("consumes a pending request exactly once", () => {
    requestFocusComposer("a-1");
    expect(consumeFocusComposerRequest("a-1")).toEqual({});
    // Single-shot: the event-listener path consuming it means the mount path
    // can't double-fire.
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
  });

  it("carries a draft to the consumer", () => {
    requestFocusComposer("a-1", "Look in our brain for: pricing");
    expect(consumeFocusComposerRequest("a-1")).toEqual({ draft: "Look in our brain for: pricing" });
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
  });

  it("returns null with no pending request", () => {
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
  });

  it("only the requested agent's composer may consume — others leave it pending", () => {
    requestFocusComposer("a-1");
    expect(consumeFocusComposerRequest("other")).toBeNull();
    // Still pending for the right agent (a different mounted composer must
    // not swallow the handoff).
    expect(consumeFocusComposerRequest("a-1")).toEqual({});
  });

  it("holds a single slot: a newer request replaces the older one", () => {
    requestFocusComposer("a-1");
    requestFocusComposer("a-2");
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
    expect(consumeFocusComposerRequest("a-2")).toEqual({});
  });

  it("expires after ~5s so a request whose panel never mounted can't fire late", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    requestFocusComposer("a-1");
    vi.setSystemTime(new Date("2026-01-01T00:00:06Z"));
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
  });

  it("consumes within the expiry window", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    requestFocusComposer("a-1");
    vi.setSystemTime(new Date("2026-01-01T00:00:04Z"));
    expect(consumeFocusComposerRequest("a-1")).toEqual({});
  });

  it("restarts expiry after deferred navigation completes", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    requestFocusComposer("a-1", "Keep me");
    deferFocusComposerRequest("a-1");
    vi.setSystemTime(new Date("2026-01-01T00:00:06Z"));
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
    expect(releaseFocusComposerRequest("a-1")).toBe(true);
    vi.setSystemTime(new Date("2026-01-01T00:00:10Z"));
    expect(consumeFocusComposerRequest("a-1")).toEqual({ draft: "Keep me" });
  });
});
