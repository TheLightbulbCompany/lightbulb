// apps/frontend/src/__tests__/teams/useAcknowledgedRuns.test.tsx
import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";

// Node 22+ ships a built-in `localStorage` that requires `--localstorage-file`
// to actually function. In our vitest+jsdom setup it shadows jsdom's
// localStorage and exposes a stub that throws on `.clear()` / `.setItem()`.
// Install a Map-backed polyfill on both `globalThis` and `Storage.prototype`
// so production code (which calls `localStorage.{getItem,setItem,...}`) and
// tests (which call `localStorage.clear()`) both work. Mirrors the polyfill
// in useReadInboxItems.test.tsx / inboxStorage.test.ts.
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

describe("useAcknowledgedRuns", () => {
  beforeEach(() => localStorage.clear());

  it("ack adds an id, isAcked reflects it, and it persists", () => {
    const { result } = renderHook(() => useAcknowledgedRuns("co1"));
    expect(result.current.isAcked("r1")).toBe(false);
    act(() => result.current.ack("r1"));
    expect(result.current.isAcked("r1")).toBe(true);
    // Persisted: a fresh hook for the same company sees it.
    const second = renderHook(() => useAcknowledgedRuns("co1"));
    expect(second.result.current.isAcked("r1")).toBe(true);
  });

  it("null companyId is a no-op", () => {
    const { result } = renderHook(() => useAcknowledgedRuns(null));
    act(() => result.current.ack("r1"));
    expect(result.current.isAcked("r1")).toBe(false);
  });
});
