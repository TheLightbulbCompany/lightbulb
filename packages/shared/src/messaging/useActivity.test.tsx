// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { ActivityItem, ActivityPage } from "./types";
import {
  activityActionRequest,
  flattenActivityPages,
  newestActivityAt,
  replaceActivityItem,
  unseenActivityCount,
  useActivity,
  type ActivityPlatform,
} from "./useActivity";

const item = (id: string, createdAt: string, over: Partial<ActivityItem> = {}): ActivityItem => ({
  id, kind: "mention", subtype: null, roomId: "room-1", messageId: `m-${id}`, threadRootMessageId: null,
  actor: { kind: "human", id: "user-b", name: "Bob" }, createdAt, state: "open",
  resolvedBy: null, resolvedAt: null, read: false,
  summary: { title: `Bob mentioned you ${id}`, requester: { kind: "human", id: "user-b", name: "Bob" }, reason: null, preview: null, links: [] },
  actions: [{ id: "approve", label: "Approve", style: "primary", confirm: null, input: null, navigate: null }],
  ...over,
});
const page = (items: ActivityItem[], next: string | null, over: Partial<ActivityPage> = {}): ActivityPage =>
  ({ items, next, unseenCount: null, sources: { paperclip: "ok" }, ...over });

const client = {
  listActivity: vi.fn<(options?: { cursor?: string | null; limit?: number }) => Promise<ActivityPage>>(),
  markActivitySeen: vi.fn(async (upTo: string) => ({ seenUpTo: upTo })),
  runActivityAction: vi.fn<(itemId: string, actionId: string, request?: object) => Promise<ActivityItem>>(),
};

const handlers = new Set<(event: LiveEvent) => void>();
const visibilityListeners = new Set<() => void>();
let visible = true;
let uuidCount = 0;
const base: ActivityPlatform = {
  userId: "user-a",
  orgId: "org-a",
  live: { onEvent: (handler) => { handlers.add(handler); return () => handlers.delete(handler); } },
  isVisible: () => visible,
  onVisibilityChange: (listener) => { visibilityListeners.add(listener); return () => visibilityListeners.delete(listener); },
  uuid: () => `uuid-${++uuidCount}`,
};
const wrapper = ({ children }: { children: ReactNode }) =>
  <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
const emit = (event: LiveEvent) => { for (const handler of handlers) handler(event); };
const available = (notify: boolean) => ({
  type: "room.message.available", eventId: `e-${Math.random()}`,
  payload: { roomId: "room-1", messageId: "m-x", messageSeq: 9, threadRootMessageId: null, notify },
}) as unknown as LiveEvent;

beforeEach(() => {
  vi.useRealTimers();
  handlers.clear();
  visibilityListeners.clear();
  visible = true;
  uuidCount = 0;
  client.listActivity.mockReset();
  client.markActivitySeen.mockClear();
  client.runActivityAction.mockReset();
  client.listActivity.mockImplementation(async (options) => options?.cursor === "c1"
    ? page([item("b", "2026-09-24T09:00:00Z")], null)
    : page([item("a", "2026-09-24T10:00:00Z")], "c1", { unseenCount: 2 }));
});

describe("pure helpers", () => {
  it("replaces an item by id and leaves the rest", () => {
    const list = [item("a", "1"), item("b", "2")];
    const next = replaceActivityItem(list, { ...list[1], state: "resolved", actions: [] });
    expect(next[0]).toBe(list[0]);
    expect(next[1].state).toBe("resolved");
    expect(replaceActivityItem(list, item("zz", "3"))).toBe(list);
  });

  it("counts items newer than seenUpTo, capped at 99", () => {
    const list = [item("a", "2026-09-24T10:00:00Z"), item("b", "2026-09-24T08:00:00Z")];
    expect(unseenActivityCount(list, "2026-09-24T09:00:00Z")).toBe(1);
    expect(unseenActivityCount(list, "2026-09-24T10:00:00.000Z")).toBe(0);
    const many = Array.from({ length: 120 }, (_, i) => item(`i${i}`, "2026-09-24T10:00:00Z"));
    expect(unseenActivityCount(many, "2026-09-01T00:00:00Z")).toBe(99);
  });

  it("finds the newest createdAt and flattens pages without duplicates", () => {
    const pages = [page([item("a", "2026-09-24T08:00:00Z"), item("b", "2026-09-24T10:00:00Z")], "c"), page([item("b", "x")], null)];
    const flat = flattenActivityPages(pages);
    expect(flat.map((i) => i.id)).toEqual(["a", "b"]);
    expect(newestActivityAt(flat)).toBe("2026-09-24T10:00:00Z");
    expect(newestActivityAt([])).toBeNull();
  });

  it("sends a text answer as both note and value, a choice as value", () => {
    const text = { kind: "text" as const, label: "Why?", required: false, options: null };
    expect(activityActionRequest(text, "because")).toEqual({ note: "because", value: "because" });
    expect(activityActionRequest({ ...text, kind: "select" }, "done")).toEqual({ value: "done" });
    expect(activityActionRequest(null)).toEqual({});
  });
});

describe("useActivity", () => {
  it("loads the first page and pages through `next`", async () => {
    const { result } = renderHook(() => useActivity(client, base), { wrapper });
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(["a"]));
    expect(result.current.unseenCount).toBe(2);
    expect(result.current.hasMore).toBe(true);
    await act(async () => { result.current.loadMore(); });
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(["a", "b"]));
    expect(client.listActivity).toHaveBeenLastCalledWith({ cursor: "c1", limit: 30 });
    expect(result.current.hasMore).toBe(false);
  });

  it("never shows one workspace's items in another", async () => {
    client.listActivity.mockImplementation(async () => page([item(`for-${scopeOrg}`, "2026-09-24T10:00:00Z")], null));
    let scopeOrg = "org-a";
    const { result, rerender } = renderHook(({ orgId }) => useActivity(client, { ...base, orgId }), {
      wrapper, initialProps: { orgId: "org-a" },
    });
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(["for-org-a"]));
    scopeOrg = "org-b";
    rerender({ orgId: "org-b" });
    expect(result.current.items.map((i) => i.id)).not.toContain("for-org-a");
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(["for-org-b"]));
  });

  it("fetches nothing while signed out", async () => {
    const { result } = renderHook(() => useActivity(client, { ...base, userId: null }), { wrapper });
    expect(result.current.items).toEqual([]);
    expect(client.listActivity).not.toHaveBeenCalled();
  });

  it("replaces the returned item in place without a refetch, minting a key per call", async () => {
    const { result } = renderHook(() => useActivity(client, base), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    const calls = client.listActivity.mock.calls.length;
    client.runActivityAction.mockResolvedValue(item("a", "2026-09-24T10:00:00Z", {
      state: "resolved", actions: [], resolvedBy: { kind: "human", id: "user-a", name: "Ann" },
    }));
    await act(async () => { await result.current.runAction("a", "approve", { note: "ok" }); });
    await act(async () => { await result.current.runAction("a", "approve"); });
    expect(client.runActivityAction).toHaveBeenNthCalledWith(1, "a", "approve", { note: "ok", idempotencyKey: "uuid-1" });
    expect(client.runActivityAction).toHaveBeenNthCalledWith(2, "a", "approve", { idempotencyKey: "uuid-2" });
    expect(result.current.items[0].state).toBe("resolved");
    expect(result.current.items[0].actions).toEqual([]);
    expect(client.listActivity.mock.calls.length).toBe(calls);
  });

  it("rejects with the ApiError and leaves the item unchanged", async () => {
    const { result } = renderHook(() => useActivity(client, base), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    const before = result.current.items[0];
    const failure = new ApiError(403, { code: "FORBIDDEN" }, "Not allowed to decide this.");
    client.runActivityAction.mockRejectedValue(failure);
    let caught: unknown;
    await act(async () => { await result.current.runAction("a", "approve").catch((e: unknown) => { caught = e; }); });
    expect(caught).toBe(failure);
    expect(result.current.items[0]).toBe(before);
  });

  it("marks seen once per newer upTo and zeroes the badge locally", async () => {
    const { result } = renderHook(() => useActivity(client, base), { wrapper });
    await waitFor(() => expect(result.current.unseenCount).toBe(2));
    await act(async () => { await result.current.markSeen("2026-09-24T10:00:00Z"); });
    await act(async () => { await result.current.markSeen("2026-09-24T10:00:00Z"); });
    expect(client.markActivitySeen).toHaveBeenCalledTimes(1);
    expect(result.current.unseenCount).toBe(0);
  });

  it("refreshes, debounced, on notifying live messages and on focus", async () => {
    const { result } = renderHook(() => useActivity(client, base, { debounceMs: 20 }), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    const calls = () => client.listActivity.mock.calls.length;
    const start = calls();
    act(() => { emit(available(false)); });
    act(() => { emit(available(true)); emit(available(true)); emit(available(true)); });
    await waitFor(() => expect(calls()).toBe(start + 1));
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(calls()).toBe(start + 1);

    visible = false;
    act(() => { for (const listener of visibilityListeners) listener(); });
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(calls()).toBe(start + 1);
    visible = true;
    act(() => { for (const listener of visibilityListeners) listener(); });
    await waitFor(() => expect(calls()).toBe(start + 2));
  });

  it("reports paperclip as unavailable from the first page", async () => {
    client.listActivity.mockResolvedValue(page([], null, { sources: { paperclip: "unavailable" }, unseenCount: 0 }));
    const { result } = renderHook(() => useActivity(client, base), { wrapper });
    await waitFor(() => expect(result.current.paperclipAvailable).toBe(false));
    expect(result.current.loading).toBe(false);
  });
});
