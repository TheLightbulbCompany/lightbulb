import {
  describe,
  test,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useReadInboxItems", () => {
  test("returns empty set when companyId is null", () => {
    const { result } = renderHook(() => useReadInboxItems(null));
    expect(result.current.readItemKeys).toBeInstanceOf(Set);
    expect(result.current.readItemKeys.size).toBe(0);
  });

  test("null companyId mutators are no-ops", () => {
    const { result } = renderHook(() => useReadInboxItems(null));
    act(() => {
      result.current.markRead("iss_1");
      result.current.markUnread("iss_1");
      result.current.markManyRead(["iss_1", "iss_2"]);
      result.current.clearAll();
    });
    expect(result.current.readItemKeys.size).toBe(0);
    // Nothing persisted under any key.
    expect(localStorage.length).toBe(0);
  });

  test("loads initial state from localStorage", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["iss_1", "iss_2"]),
    );
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    expect(result.current.isRead("iss_1")).toBe(true);
    expect(result.current.isRead("iss_2")).toBe(true);
    expect(result.current.isRead("iss_3")).toBe(false);
    expect(result.current.readItemKeys.size).toBe(2);
  });

  test("markRead adds to set + persists", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    act(() => result.current.markRead("iss_1"));
    expect(result.current.isRead("iss_1")).toBe(true);
    const raw = localStorage.getItem("paperclip:inbox:co_1:read-items");
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string)).toEqual(["iss_1"]);
  });

  test("markRead is idempotent — calling twice does not duplicate", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    act(() => result.current.markRead("iss_1"));
    act(() => result.current.markRead("iss_1"));
    expect(result.current.readItemKeys.size).toBe(1);
  });

  test("markUnread removes from set + persists", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["iss_1", "iss_2"]),
    );
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    act(() => result.current.markUnread("iss_1"));
    expect(result.current.isRead("iss_1")).toBe(false);
    expect(result.current.isRead("iss_2")).toBe(true);
    const raw = localStorage.getItem("paperclip:inbox:co_1:read-items");
    expect(JSON.parse(raw as string)).toEqual(["iss_2"]);
  });

  test("markUnread on absent id is a no-op", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    const before = result.current.readItemKeys;
    act(() => result.current.markUnread("iss_x"));
    // Same Set instance — no spurious re-render churn.
    expect(result.current.readItemKeys).toBe(before);
  });

  test("markManyRead is single update + single save", () => {
    // localStorage is jsdom's real Storage, so setItem lives on the prototype
    // and a prototype spy observes every write. (This used to assign an own
    // `localStorage.setItem`, which only worked because the file installed a
    // plain-object stand-in whose methods shadowed the prototype.)
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    setItem.mockClear(); // ignore any setItem from initial render
    act(() => result.current.markManyRead(["iss_1", "iss_2", "iss_3"]));
    expect(result.current.readItemKeys.size).toBe(3);
    expect(result.current.isRead("iss_1")).toBe(true);
    expect(result.current.isRead("iss_2")).toBe(true);
    expect(result.current.isRead("iss_3")).toBe(true);
    const readItemWrites = setItem.mock.calls.filter(
      ([k]) => k === "paperclip:inbox:co_1:read-items",
    );
    expect(readItemWrites.length).toBe(1);
    // And the single write contains all three ids.
    expect(JSON.parse(readItemWrites[0][1])).toEqual(
      expect.arrayContaining(["iss_1", "iss_2", "iss_3"]),
    );
  });

  test("markManyRead with all already-read ids is a no-op", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["iss_1", "iss_2"]),
    );
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    const before = result.current.readItemKeys;
    act(() => result.current.markManyRead(["iss_1", "iss_2"]));
    expect(result.current.readItemKeys).toBe(before);
  });

  test("clearAll empties + persists", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["iss_1", "iss_2"]),
    );
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    expect(result.current.readItemKeys.size).toBe(2);
    act(() => result.current.clearAll());
    expect(result.current.readItemKeys.size).toBe(0);
    const raw = localStorage.getItem("paperclip:inbox:co_1:read-items");
    expect(JSON.parse(raw as string)).toEqual([]);
  });

  test("clearAll on already-empty set is a no-op", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    const before = result.current.readItemKeys;
    act(() => result.current.clearAll());
    expect(result.current.readItemKeys).toBe(before);
  });

  test("storage event from another tab updates state", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    expect(result.current.isRead("iss_x")).toBe(false);
    act(() => {
      // Simulate another tab writing then dispatching the storage event.
      localStorage.setItem(
        "paperclip:inbox:co_1:read-items",
        JSON.stringify(["iss_x"]),
      );
      const event = new StorageEvent("storage", {
        key: "paperclip:inbox:co_1:read-items",
      });
      window.dispatchEvent(event);
    });
    expect(result.current.isRead("iss_x")).toBe(true);
  });

  test("storage event for unrelated key is ignored", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    act(() => {
      const event = new StorageEvent("storage", { key: "unrelated" });
      window.dispatchEvent(event);
    });
    expect(result.current.readItemKeys.size).toBe(0);
  });

  test("storage event for a different company's key is ignored", () => {
    const { result } = renderHook(() => useReadInboxItems("co_1"));
    act(() => {
      localStorage.setItem(
        "paperclip:inbox:co_2:read-items",
        JSON.stringify(["iss_x"]),
      );
      const event = new StorageEvent("storage", {
        key: "paperclip:inbox:co_2:read-items",
      });
      window.dispatchEvent(event);
    });
    expect(result.current.readItemKeys.size).toBe(0);
  });

  test("changing companyId reloads state", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["a"]),
    );
    localStorage.setItem(
      "paperclip:inbox:co_2:read-items",
      JSON.stringify(["b"]),
    );
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useReadInboxItems(id),
      { initialProps: { id: "co_1" as string | null } },
    );
    expect(result.current.isRead("a")).toBe(true);
    expect(result.current.isRead("b")).toBe(false);
    rerender({ id: "co_2" });
    expect(result.current.isRead("a")).toBe(false);
    expect(result.current.isRead("b")).toBe(true);
  });

  test("transitioning companyId from null to id loads stored state", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["iss_1"]),
    );
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useReadInboxItems(id),
      { initialProps: { id: null as string | null } },
    );
    expect(result.current.readItemKeys.size).toBe(0);
    rerender({ id: "co_1" });
    expect(result.current.isRead("iss_1")).toBe(true);
  });
});
