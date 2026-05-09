import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useInboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";

// Node 22+ ships a built-in `localStorage` that requires `--localstorage-file`
// to actually function. In our vitest+jsdom setup it shadows jsdom's
// localStorage and exposes a stub that throws on `.clear()` / `.setItem()`.
// Install a Map-backed polyfill on both `globalThis` and `Storage.prototype`
// so production code (which calls `localStorage.{getItem,setItem,...}`) and
// tests (which `vi.spyOn(Storage.prototype, ...)`) both work.
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

describe("useInboxViewMode", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to 'list' with no stored value", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });

  it("reads a stored value", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "board");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("board");
  });

  it("writes on setViewMode", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    act(() => result.current.setViewMode("board"));
    expect(result.current.viewMode).toBe("board");
    expect(window.localStorage.getItem("paperclip:inbox:co-1:view-mode")).toBe("board");
  });

  it("scopes per companyId", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "board");
    const { result } = renderHook(() => useInboxViewMode("co-2"));
    expect(result.current.viewMode).toBe("list");
  });

  it("returns 'list' with null companyId without touching localStorage", () => {
    const { result } = renderHook(() => useInboxViewMode(null));
    expect(result.current.viewMode).toBe("list");
    act(() => result.current.setViewMode("board"));
    expect(window.localStorage.length).toBe(0);
  });

  it("ignores unknown stored values", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "garbage");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });

  it("re-reads storage when companyId changes", () => {
    window.localStorage.setItem("paperclip:inbox:co-1:view-mode", "board");
    const { result, rerender } = renderHook(
      ({ id }) => useInboxViewMode(id),
      { initialProps: { id: "co-2" as string | null } },
    );
    expect(result.current.viewMode).toBe("list");
    rerender({ id: "co-1" });
    expect(result.current.viewMode).toBe("board");
  });
});
