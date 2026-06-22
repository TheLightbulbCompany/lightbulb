import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import { UNIFIED_NAV_KEY, readUnifiedNav, applyUnifiedNavParam } from "../unifiedNav";

// Node 22+ ships a built-in `localStorage` that requires `--localstorage-file`
// to actually function. In our vitest+jsdom setup it shadows jsdom's
// localStorage and exposes a stub that throws on `.clear()` / `.setItem()`.
// Install a Map-backed polyfill on both `globalThis` and `Storage.prototype`
// so production code (which calls `localStorage.{getItem,setItem,...}`) and
// tests (which read it back) both work.
beforeAll(() => {
  const store = new Map<string, string>();
  const polyfill: Storage = {
    getItem: (k) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k, v) => {
      store.set(k, String(v));
    },
    removeItem: (k) => {
      store.delete(k);
    },
    clear: () => {
      store.clear();
    },
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
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
  Storage.prototype.getItem = function (k: string) {
    return polyfill.getItem(k);
  };
  Storage.prototype.setItem = function (k: string, v: string) {
    polyfill.setItem(k, v);
  };
  Storage.prototype.removeItem = function (k: string) {
    polyfill.removeItem(k);
  };
  Storage.prototype.clear = function () {
    polyfill.clear();
  };
});

describe("unifiedNav storage helpers", () => {
  beforeEach(() => window.localStorage.clear());

  it("reads false when unset", () => {
    expect(readUnifiedNav()).toBe(false);
  });
  it("reads true when the key is '1'", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "1");
    expect(readUnifiedNav()).toBe(true);
  });
  it("applyUnifiedNavParam('1') enables + returns true", () => {
    expect(applyUnifiedNavParam("1")).toBe(true);
    expect(window.localStorage.getItem(UNIFIED_NAV_KEY)).toBe("1");
  });
  it("applyUnifiedNavParam('0') clears + returns false", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "1");
    expect(applyUnifiedNavParam("0")).toBe(false);
    expect(window.localStorage.getItem(UNIFIED_NAV_KEY)).toBeNull();
  });
  it("applyUnifiedNavParam(null) is a no-op returning null", () => {
    expect(applyUnifiedNavParam(null)).toBeNull();
  });
});
