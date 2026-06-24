import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import {
  UNIFIED_NAV_KEY,
  readUnifiedNav,
  applyUnifiedNavParam,
  resolveUnifiedNav,
} from "../unifiedNav";

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

// The unified nav is now the DEFAULT everywhere. The only stored value that
// disables it is the explicit opt-out marker "0"; `?unifiedNav=0` persists that
// marker and `?unifiedNav=1` clears it (back to the default).
describe("unifiedNav storage helpers", () => {
  beforeEach(() => window.localStorage.clear());

  it("reads true when unset (default ON)", () => {
    expect(readUnifiedNav()).toBe(true);
  });
  it("reads false only when the opt-out key is '0'", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "0");
    expect(readUnifiedNav()).toBe(false);
  });
  it("reads true for any non-'0' stored value", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "1");
    expect(readUnifiedNav()).toBe(true);
  });
  it("applyUnifiedNavParam('0') persists the opt-out + returns false", () => {
    expect(applyUnifiedNavParam("0")).toBe(false);
    expect(window.localStorage.getItem(UNIFIED_NAV_KEY)).toBe("0");
  });
  it("applyUnifiedNavParam('1') clears the opt-out + returns true", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "0");
    expect(applyUnifiedNavParam("1")).toBe(true);
    expect(window.localStorage.getItem(UNIFIED_NAV_KEY)).toBeNull();
  });
  it("applyUnifiedNavParam(null) is a no-op returning null", () => {
    expect(applyUnifiedNavParam(null)).toBeNull();
  });
});

describe("resolveUnifiedNav (URL param overrides the stored value)", () => {
  it("opt-out param wins over a stored ON", () => {
    expect(resolveUnifiedNav("0", true)).toBe(false);
  });
  it("opt-in param wins over a stored OFF", () => {
    expect(resolveUnifiedNav("1", false)).toBe(true);
  });
  it("falls back to the stored value for an absent/invalid param", () => {
    expect(resolveUnifiedNav(null, true)).toBe(true);
    expect(resolveUnifiedNav(null, false)).toBe(false);
    expect(resolveUnifiedNav("bogus", true)).toBe(true);
    expect(resolveUnifiedNav("bogus", false)).toBe(false);
  });
});
