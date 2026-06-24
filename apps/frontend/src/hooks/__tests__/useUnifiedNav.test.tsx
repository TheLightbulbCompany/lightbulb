import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { UNIFIED_NAV_KEY } from "@/lib/unifiedNav";

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

// A single mutable URLSearchParams the mock returns each render.
const params = new URLSearchParams();
vi.mock("next/navigation", () => ({ useSearchParams: () => params }));

import { useUnifiedNav } from "../useUnifiedNav";

describe("useUnifiedNav", () => {
  beforeEach(() => {
    window.localStorage.clear();
    params.delete("unifiedNav");
  });

  it("defaults to true (unified nav is the default)", () => {
    expect(renderHook(() => useUnifiedNav()).result.current).toBe(true);
  });
  it("reads false from an explicit opt-out in localStorage", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "0");
    expect(renderHook(() => useUnifiedNav()).result.current).toBe(false);
  });
  it("?unifiedNav=0 opts out + persists the opt-out", () => {
    params.set("unifiedNav", "0");
    expect(renderHook(() => useUnifiedNav()).result.current).toBe(false);
    expect(window.localStorage.getItem(UNIFIED_NAV_KEY)).toBe("0");
  });
  it("?unifiedNav=1 opts back in + clears the opt-out", () => {
    window.localStorage.setItem(UNIFIED_NAV_KEY, "0");
    params.set("unifiedNav", "1");
    expect(renderHook(() => useUnifiedNav()).result.current).toBe(true);
    expect(window.localStorage.getItem(UNIFIED_NAV_KEY)).toBeNull();
  });
});
