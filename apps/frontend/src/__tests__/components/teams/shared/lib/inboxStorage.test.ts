import { describe, test, expect, beforeAll, beforeEach, afterEach, vi } from "vitest";
import {
  inboxStorageKey,
  loadAckRuns,
  saveAckRuns,
} from "@/components/teams/shared/lib/inboxStorage";

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
  // Route Storage.prototype methods through the polyfill so vi.spyOn hooks
  // observed by `localStorage.getItem(...)` calls actually fire.
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

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("inboxStorageKey", () => {
  test("namespaces per company", () => {
    expect(inboxStorageKey("co_123", "filters")).toBe("paperclip:inbox:co_123:filters");
  });

  test("namespaces other keys identically", () => {
    expect(inboxStorageKey("co_abc", "tab")).toBe("paperclip:inbox:co_abc:tab");
    expect(inboxStorageKey("co_abc", "read-items")).toBe(
      "paperclip:inbox:co_abc:read-items",
    );
  });
});

describe("triage storage helpers", () => {
  beforeEach(() => localStorage.clear());

  test("ack-runs round-trips a Set and isolates per company", () => {
    saveAckRuns("co1", new Set(["r1", "r2"]));
    expect(loadAckRuns("co1")).toEqual(new Set(["r1", "r2"]));
    expect(loadAckRuns("co2")).toEqual(new Set()); // per-company isolation
  });

  test("ack-runs returns empty Set when missing or malformed", () => {
    expect(loadAckRuns("missing")).toEqual(new Set());
    localStorage.setItem("paperclip:inbox:bad:ack-runs", "{not json");
    expect(loadAckRuns("bad")).toEqual(new Set());
  });
});
