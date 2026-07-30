import { describe, test, expect, beforeEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

// Mocks BEFORE imports
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, useSWRConfig: vi.fn() };
});

import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useSWRConfig } from "swr";

const mockPost = vi.fn();
const mockMutate = vi.fn();
const mockCacheGet = vi.fn();
const mockCache = {
  get: mockCacheGet,
  set: vi.fn(),
  delete: vi.fn(),
  keys: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useTeamsApi).mockReturnValue({
    read: vi.fn(),
    post: mockPost,
    patch: vi.fn(),
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
  vi.mocked(useSWRConfig).mockReturnValue({
    cache: mockCache,
    mutate: mockMutate,
  } as unknown as ReturnType<typeof useSWRConfig>);
});

describe("useInboxArchiveStack", () => {
  test("starts empty + hasUndoableArchive=false", () => {
    const { result } = renderHook(() => useInboxArchiveStack());
    expect(result.current.archivingIssueIds.size).toBe(0);
    expect(result.current.hasUndoableArchive).toBe(false);
  });

  test("archive(id) optimistically removes from all 3 inbox keys + posts to BFF (no settle on success)", async () => {
    mockPost.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.archive("iss_1");
    });
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_1/archive", {});
    // Success path: 3 optimistic mutates only — no settle revalidate.
    // Settle-on-success raced eventual consistency; reviewer (#3c) flagged this.
    expect(mockMutate).toHaveBeenCalledTimes(3);
  });

  test("archive succeeds → pushes onto undo stack", async () => {
    mockPost.mockResolvedValue({});
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.archive("iss_1");
    });
    expect(result.current.hasUndoableArchive).toBe(true);
  });

  test("archive errors → rollback + does NOT add to undo stack", async () => {
    mockCacheGet.mockReturnValue({
      data: { items: [{ id: "iss_1", title: "x", status: "todo" }] },
    });
    mockPost.mockRejectedValue(new Error("server boom"));
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await expect(result.current.archive("iss_1")).rejects.toThrow(
        "server boom",
      );
    });
    expect(result.current.hasUndoableArchive).toBe(false);
  });

  test("archive error → rollback restores cache snapshots and settles", async () => {
    const snapshot = { items: [{ id: "iss_1", title: "x", status: "todo" }] };
    mockCacheGet.mockReturnValue({ data: snapshot });
    mockPost.mockRejectedValue(new Error("server boom"));
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await expect(result.current.archive("iss_1")).rejects.toThrow(
        "server boom",
      );
    });
    // Find the rollback calls — they pass the literal snapshot object as the
    // second arg (not a function). Three rollbacks (one per inbox key).
    const rollbackCalls = mockMutate.mock.calls.filter(
      ([, value]) => value === snapshot,
    );
    expect(rollbackCalls).toHaveLength(3);
    // And on the error path we settle with a single-arg mutate (revalidate)
    // for each inbox key.
    const settleCalls = mockMutate.mock.calls.filter(
      ([, value]) => value === undefined,
    );
    expect(settleCalls).toHaveLength(3);
  });

  test("archivingIssueIds tracks in-flight ids during await", async () => {
    let resolvePost: (v: unknown) => void = () => {};
    mockPost.mockImplementation(
      () =>
        new Promise((res) => {
          resolvePost = res;
        }),
    );
    const { result } = renderHook(() => useInboxArchiveStack());
    let archivePromise: Promise<void>;
    act(() => {
      archivePromise = result.current.archive("iss_1");
    });
    await waitFor(() =>
      expect(result.current.archivingIssueIds.has("iss_1")).toBe(true),
    );
    await act(async () => {
      resolvePost({});
      await archivePromise!;
    });
    expect(result.current.archivingIssueIds.has("iss_1")).toBe(false);
  });

  test("undoArchive pops from stack + posts unarchive (LIFO)", async () => {
    mockPost.mockResolvedValue({});
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.archive("iss_1");
    });
    await act(async () => {
      await result.current.archive("iss_2");
    });
    expect(result.current.hasUndoableArchive).toBe(true);

    mockPost.mockClear();
    await act(async () => {
      await result.current.undoArchive();
    });
    // Most recent (iss_2) is undone first
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_2/unarchive", {});
  });

  test("undoArchive on empty stack is a no-op", async () => {
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.undoArchive();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  test("undoArchive failure restores the popped id to the stack", async () => {
    mockPost.mockResolvedValueOnce({}); // archive succeeds
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.archive("iss_1");
    });
    expect(result.current.hasUndoableArchive).toBe(true);

    mockPost.mockRejectedValueOnce(new Error("unarchive failed"));
    await act(async () => {
      await expect(result.current.undoArchive()).rejects.toThrow(
        "unarchive failed",
      );
    });
    expect(result.current.hasUndoableArchive).toBe(true); // restored
  });

  test("concurrent archives both land on the undo stack (functional updater preserves both ids)", async () => {
    // Hold both posts open so the two archive() calls overlap. Without
    // functional setState updaters, the second archive's setUndoStack would
    // close over the pre-first-archive snapshot and clobber `iss_a`.
    let resolveA: (v: unknown) => void = () => {};
    let resolveB: (v: unknown) => void = () => {};
    mockPost
      .mockImplementationOnce(
        () => new Promise((res) => (resolveA = res)),
      )
      .mockImplementationOnce(
        () => new Promise((res) => (resolveB = res)),
      );

    const { result } = renderHook(() => useInboxArchiveStack());

    let archiveAPromise: Promise<void>;
    let archiveBPromise: Promise<void>;
    act(() => {
      archiveAPromise = result.current.archive("iss_a");
      archiveBPromise = result.current.archive("iss_b");
    });
    // Both posts fired
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_a/archive", {});
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_b/archive", {});

    await act(async () => {
      resolveA({});
      resolveB({});
      await archiveAPromise!;
      await archiveBPromise!;
    });
    // Both ids end up on the undo stack — popping should yield iss_b first,
    // then iss_a.
    mockPost.mockClear();
    mockPost.mockResolvedValue({});
    await act(async () => {
      await result.current.undoArchive();
    });
    expect(mockPost).toHaveBeenLastCalledWith("/issues/iss_b/unarchive", {});
    await act(async () => {
      await result.current.undoArchive();
    });
    expect(mockPost).toHaveBeenLastCalledWith("/issues/iss_a/unarchive", {});
    // Stack is now empty.
    expect(result.current.hasUndoableArchive).toBe(false);
  });

  test("markRead optimistically flips unread → false in cache", async () => {
    mockPost.mockResolvedValue({});
    mockCacheGet.mockReturnValue({
      data: { items: [{ id: "iss_1", title: "x", status: "todo", unread: true }] },
    });
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.markRead("iss_1");
    });
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_1/mark-read", {});
    // Find the optimistic mutate calls — second arg is a function. Apply it
    // to the cached snapshot and check that unread flipped to false.
    const optimistic = mockMutate.mock.calls.filter(
      ([, value]) => typeof value === "function",
    );
    expect(optimistic.length).toBeGreaterThanOrEqual(3);
    const updater = optimistic[0][1] as (
      d: { items: Array<{ id: string; unread: boolean }> } | undefined,
    ) => { items: Array<{ id: string; unread: boolean }> };
    const result2 = updater({
      items: [{ id: "iss_1", unread: true }],
    });
    expect(result2.items[0].unread).toBe(false);
  });

  test("markRead error rolls back to original cache state", async () => {
    const snapshot = {
      items: [{ id: "iss_1", title: "x", status: "todo", unread: true }],
    };
    mockCacheGet.mockReturnValue({ data: snapshot });
    mockPost.mockRejectedValue(new Error("mark-read boom"));
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await expect(result.current.markRead("iss_1")).rejects.toThrow(
        "mark-read boom",
      );
    });
    // Three rollback calls passing the snapshot back into mutate.
    const rollbackCalls = mockMutate.mock.calls.filter(
      ([, value]) => value === snapshot,
    );
    expect(rollbackCalls).toHaveLength(3);
  });

  test("markUnread optimistically flips unread → true in cache", async () => {
    mockPost.mockResolvedValue({});
    mockCacheGet.mockReturnValue({
      data: { items: [{ id: "iss_1", title: "x", status: "todo", unread: false }] },
    });
    const { result } = renderHook(() => useInboxArchiveStack());
    await act(async () => {
      await result.current.markUnread("iss_1");
    });
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_1/mark-unread", {});
    const optimistic = mockMutate.mock.calls.filter(
      ([, value]) => typeof value === "function",
    );
    expect(optimistic.length).toBeGreaterThanOrEqual(3);
    const updater = optimistic[0][1] as (
      d: { items: Array<{ id: string; unread: boolean }> } | undefined,
    ) => { items: Array<{ id: string; unread: boolean }> };
    const result2 = updater({
      items: [{ id: "iss_1", unread: false }],
    });
    expect(result2.items[0].unread).toBe(true);
  });
});
