import { describe, it, expect, beforeEach, beforeAll } from "vitest";
import {
  trackRecentAssignee,
  getRecentAssignees,
  RECENT_ASSIGNEES_KEY,
} from "@/components/teams/issues/pickers/recent-assignees";

// Node 22+ ships a built-in `localStorage` that requires `--localstorage-file`
// to actually function. In our vitest+jsdom setup it shadows jsdom's
// localStorage and exposes a stub that throws on `.clear()` / `.setItem()`.
// Install a Map-backed polyfill (same pattern as inboxStorage.test.ts).
beforeAll(() => {
  const store = new Map<string, string>();
  const polyfill: Storage = {
    getItem: (k) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => { store.clear(); },
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: polyfill,
    writable: true,
    configurable: true,
  });
  if (typeof window !== "undefined") {
    Object.defineProperty(window, "localStorage", {
      value: polyfill,
      writable: true,
      configurable: true,
    });
  }
  Storage.prototype.getItem = function (k: string) { return polyfill.getItem(k); };
  Storage.prototype.setItem = function (k: string, v: string) { polyfill.setItem(k, v); };
  Storage.prototype.removeItem = function (k: string) { polyfill.removeItem(k); };
  Storage.prototype.clear = function () { polyfill.clear(); };
});

describe("recent-assignees", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("starts empty", () => {
    expect(getRecentAssignees()).toEqual([]);
  });

  it("tracks an id and returns it on read", () => {
    trackRecentAssignee("ag_1");
    expect(getRecentAssignees()).toEqual(["ag_1"]);
  });

  it("most-recently-tracked appears first", () => {
    trackRecentAssignee("ag_1");
    trackRecentAssignee("ag_2");
    trackRecentAssignee("ag_3");
    expect(getRecentAssignees()).toEqual(["ag_3", "ag_2", "ag_1"]);
  });

  it("re-tracking an existing id moves it to the front without duplicating", () => {
    trackRecentAssignee("ag_1");
    trackRecentAssignee("ag_2");
    trackRecentAssignee("ag_1");
    expect(getRecentAssignees()).toEqual(["ag_1", "ag_2"]);
  });

  it("caps the list at 5 entries (oldest dropped)", () => {
    ["a", "b", "c", "d", "e", "f"].forEach(trackRecentAssignee);
    const recent = getRecentAssignees();
    expect(recent).toHaveLength(5);
    expect(recent[0]).toBe("f");
    expect(recent).not.toContain("a");
  });

  it("survives JSON-corrupt localStorage by returning empty + clearing", () => {
    localStorage.setItem(RECENT_ASSIGNEES_KEY, "{not valid json");
    expect(getRecentAssignees()).toEqual([]);
    expect(localStorage.getItem(RECENT_ASSIGNEES_KEY)).toBeNull();
  });

  it("returns empty when localStorage is unavailable (SSR-safe)", () => {
    // Simulate SSR by pretending window doesn't exist. Cast through a
    // partial type so the delete on globalThis.window typechecks.
    const realWindow = globalThis.window;
    delete (globalThis as { window?: Window }).window;
    try {
      expect(getRecentAssignees()).toEqual([]);
      // track is a no-op in SSR; should not throw
      expect(() => trackRecentAssignee("ag_1")).not.toThrow();
    } finally {
      globalThis.window = realWindow;
    }
  });
});
