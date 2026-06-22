import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from "vitest";
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

describe("resolveUnifiedNav (URL param overrides the stored value)", () => {
  it("opt-out param wins over a stored '1'", () => {
    expect(resolveUnifiedNav("0", true)).toBe(false);
  });
  it("opt-in param wins over no stored value", () => {
    expect(resolveUnifiedNav("1", false)).toBe(true);
  });
  it("falls back to the stored value for an absent/invalid param", () => {
    expect(resolveUnifiedNav(null, true)).toBe(true);
    expect(resolveUnifiedNav(null, false)).toBe(false);
    expect(resolveUnifiedNav("bogus", true)).toBe(true);
  });
});

// The unified-nav flag is a dev/preview-only escape hatch — it must never read
// or write the localStorage key on the production apex (api.isol8.co). The gate
// keys off NEXT_PUBLIC_API_URL containing `//api.isol8.co`, which excludes
// api-dev., api-staging., and localhost so dev + Vercel previews keep full
// access.
describe("dev-only prod flag gate", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.unstubAllEnvs());

  it("on the prod apex, all three helpers are inert no-ops", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.isol8.co/api/v1");
    // Even with the key explicitly set, reads return false…
    window.localStorage.setItem(UNIFIED_NAV_KEY, "1");
    expect(readUnifiedNav()).toBe(false);
    // …writes are suppressed (the param-apply is a null no-op)…
    expect(applyUnifiedNavParam("1")).toBeNull();
    // …and resolution short-circuits to false regardless of param/stored.
    expect(resolveUnifiedNav("1", true)).toBe(false);
  });

  it("does NOT suppress the key on api-dev. (substring guard)", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api-dev.isol8.co/api/v1");
    expect(applyUnifiedNavParam("1")).toBe(true);
    expect(readUnifiedNav()).toBe(true);
    expect(resolveUnifiedNav("1", false)).toBe(true);
  });

  it("does NOT suppress the key on api-staging. or localhost", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api-staging.isol8.co/api/v1");
    expect(applyUnifiedNavParam("1")).toBe(true);
    expect(readUnifiedNav()).toBe(true);
    vi.stubEnv("NEXT_PUBLIC_API_URL", "http://localhost:8000/api/v1");
    expect(resolveUnifiedNav(null, true)).toBe(true);
  });
});
