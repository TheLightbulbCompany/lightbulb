/**
 * Guards what tests/setup.ts does to web storage.
 *
 * Both properties below have been broken here before, and neither failure is
 * visible from a passing suite: storage that silently no-ops makes every
 * storage-backed branch untested, and storage that survives a test makes the
 * next one start from a state nobody wrote down.
 */
import { describe, it, expect, vi } from "vitest";

describe("web storage test setup", () => {
  it("exposes jsdom's real Storage, so prototype spies observe real calls", () => {
    // The requirement: `vi.spyOn(Storage.prototype, ...)` — the idiomatic way
    // to stub storage failure — must actually fire. A stand-in whose methods
    // are own properties shadows the prototype, and every assertion written
    // against such a spy then passes for the wrong reason.
    expect(localStorage).toBeInstanceOf(Storage);

    const setSpy = vi.spyOn(Storage.prototype, "setItem");
    localStorage.setItem("guard", "1");
    expect(setSpy).toHaveBeenCalledWith("guard", "1");
    setSpy.mockRestore();

    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    expect(() => localStorage.getItem("guard")).toThrow("storage unavailable");
  });

  it("starts each test with an empty store", () => {
    // Depends on the afterEach clear in tests/setup.ts: the test above wrote
    // "guard" and this one must not see it.
    expect(localStorage.getItem("guard")).toBeNull();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
