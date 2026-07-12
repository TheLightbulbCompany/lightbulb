import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  requestFocusComposer,
  consumeFocusComposerRequest,
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
    expect(consumeFocusComposerRequest("a-1")).toBe(true);
    // Single-shot: the event-listener path consuming it means the mount path
    // can't double-fire.
    expect(consumeFocusComposerRequest("a-1")).toBe(false);
  });

  it("returns false with no pending request", () => {
    expect(consumeFocusComposerRequest("a-1")).toBe(false);
  });

  it("only the requested agent's composer may consume — others leave it pending", () => {
    requestFocusComposer("a-1");
    expect(consumeFocusComposerRequest("other")).toBe(false);
    // Still pending for the right agent (a different mounted composer must
    // not swallow the handoff).
    expect(consumeFocusComposerRequest("a-1")).toBe(true);
  });

  it("holds a single slot: a newer request replaces the older one", () => {
    requestFocusComposer("a-1");
    requestFocusComposer("a-2");
    expect(consumeFocusComposerRequest("a-1")).toBe(false);
    expect(consumeFocusComposerRequest("a-2")).toBe(true);
  });

  it("expires after ~5s so a request whose panel never mounted can't fire late", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    requestFocusComposer("a-1");
    vi.setSystemTime(new Date("2026-01-01T00:00:06Z"));
    expect(consumeFocusComposerRequest("a-1")).toBe(false);
  });

  it("consumes within the expiry window", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    requestFocusComposer("a-1");
    vi.setSystemTime(new Date("2026-01-01T00:00:04Z"));
    expect(consumeFocusComposerRequest("a-1")).toBe(true);
  });
});
