// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useLocalSessionHistory, type SessionSource } from "./useLocalSessionHistory";
import { useWorkspaceSessions } from "./useWorkspaceSessions";
import type { LocalSessionEvent } from "./types";

const ev = (seq: number): LocalSessionEvent => ({ seq, kind: "text", role: "assistant", text: `e${seq}`, truncated: false });

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("useLocalSessionHistory hint feed", () => {
  it("a workspace.sessions.changed push bumps useWorkspaceSessions' hintRevision with no timer advance, feeding a history load(\"newer\")", async () => {
    vi.useFakeTimers();
    const handlers = new Set<(event: unknown) => void>();
    const listWorkspaceSessions = vi.fn(async () => ({ sessions: [] }));
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null }) // initial "latest" history load
      .mockResolvedValueOnce({ events: [ev(4)], nextAfterSeq: null }); // the "newer" load the push's revision bump feeds
    const pushSource = {
      client: { listWorkspaceSessions, listLocalSessionEvents, submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
      onEvent: (handler: (event: unknown) => void) => { handlers.add(handler); return () => handlers.delete(handler); },
      connectionRevision: 1,
    } as unknown as SessionSource;
    const onAccessLost = () => {}; // stable reference: an inline fn would re-identity `load` every render.
    const { result } = renderHook(() => {
      const sessions = useWorkspaceSessions(pushSource);
      // The row's latestEventSeq matches the loaded page, so only the hint feeds the next load.
      const history = useLocalSessionHistory(pushSource.client, "session-1", 3, sessions.hintRevision, onAccessLost);
      return { sessions, history };
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); }); // flush; no polling elapsed
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
    expect(result.current.history.events).toHaveLength(3);

    // A transcript hint: the list is not refetched, but the open history pulls newer events.
    await act(async () => {
      for (const handler of handlers) handler({ type: "workspace.sessions.changed", payload: { humanUserId: "user-1", transcript: true }, schemaVersion: 1, eventId: "e1", occurredAt: "now" } as never);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1);
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2);
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 3, 50);
    expect(result.current.history.events).toHaveLength(4);
  });
});

describe("useLocalSessionHistory tail stability", () => {
  it("keeps the same events array across an empty poll, and hands out a new one when an event arrives", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [ev(4)], nextAfterSeq: null });
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {}; // stable reference: an inline fn would re-identity `load` every render.
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 3, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.events).toHaveLength(3);
    const afterInitial = result.current.events;

    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.events).toBe(afterInitial);

    rerender({ revision: 2 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.events).not.toBe(afterInitial);
    expect(result.current.events).toHaveLength(4);
  });

  it("never flashes loading on a background revision-bump load, and new events still arrive", async () => {
    vi.useFakeTimers();
    let resolveRevision!: (v: { events: LocalSessionEvent[]; nextAfterSeq: number | null }) => void;
    const revisionFetch = new Promise<{ events: LocalSessionEvent[]; nextAfterSeq: number | null }>(resolve => { resolveRevision = resolve; });
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null })
      .mockReturnValueOnce(revisionFetch);
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 3, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toHaveLength(3);

    // The revision effect, driven by useWorkspaceSessions' push hint.
    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false); // stayed false while the fetch is in flight
    await act(async () => { resolveRevision({ events: [ev(4)], nextAfterSeq: null }); await revisionFetch; });
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toHaveLength(4);
  });

  it("still flashes loading for a user-initiated load (Load older/newer, Jump to latest, retry)", async () => {
    vi.useFakeTimers();
    let resolveManual!: (v: { events: LocalSessionEvent[]; nextAfterSeq: number | null }) => void;
    const manual = new Promise<{ events: LocalSessionEvent[]; nextAfterSeq: number | null }>(resolve => { resolveManual = resolve; });
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null })
      .mockReturnValueOnce(manual);
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result } = renderHook(({ revision }) => useLocalSessionHistory(client, "session-1", 3, revision, onAccessLost), { initialProps: { revision: 0 } });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false);

    act(() => { void result.current.load("latest"); }); // e.g. "Jump to latest"
    expect(result.current.loading).toBe(true);
    await act(async () => { resolveManual({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null }); await manual; });
    expect(result.current.loading).toBe(false);
  });

  it("a foreground load supersedes an in-flight quiet background load instead of being dropped", async () => {
    vi.useFakeTimers();
    let resolveQuiet!: (v: { events: LocalSessionEvent[]; nextAfterSeq: number | null }) => void;
    const quietFetch = new Promise<{ events: LocalSessionEvent[]; nextAfterSeq: number | null }>(resolve => { resolveQuiet = resolve; });
    let resolveForeground!: (v: { events: LocalSessionEvent[]; nextAfterSeq: number | null }) => void;
    const foregroundFetch = new Promise<{ events: LocalSessionEvent[]; nextAfterSeq: number | null }>(resolve => { resolveForeground = resolve; });
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(48), ev(49), ev(50)], nextAfterSeq: null }) // initial "latest"
      .mockReturnValueOnce(quietFetch) // background "newer" poll (quiet)
      .mockReturnValueOnce(foregroundFetch); // the superseding "older" load
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 50, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false);
    expect(result.current.hasOlder).toBe(true); // "Load older" is on screen

    rerender({ revision: 1 }); // the revision-driven quiet background poll, now pending on quietFetch
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false); // quiet: not flashing

    act(() => { void result.current.load("older"); }); // a "Load older" click while the quiet poll is in flight
    expect(result.current.loading).toBe(true); // the foreground request was issued, not dropped
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3);

    await act(async () => { resolveForeground({ events: [ev(47)], nextAfterSeq: null }); await foregroundFetch; });
    expect(result.current.loading).toBe(false);
    expect(result.current.events.some(e => e.seq === 47)).toBe(true); // its events landed
    const eventsAfterForeground = result.current.events;

    // The stale quiet promise resolving afterwards changes nothing.
    await act(async () => { resolveQuiet({ events: [ev(51)], nextAfterSeq: null }); await quietFetch; });
    expect(result.current.events).toBe(eventsAfterForeground);
    expect(result.current.loading).toBe(false);
  });

  it("polls quietly for newer events on a steady cadence when pollMs is set, and stops on unmount", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2)], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [ev(3)], nextAfterSeq: null });
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, unmount } = renderHook(
      () => useLocalSessionHistory(client, "session-1", 2, 0, onAccessLost, { pollMs: 5000 }),
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false); // quiet: no spinner
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3);
    expect(result.current.events).toHaveLength(3);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3); // interval cleared
  });

  it("does not poll when pollMs is unset", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn(async () => ({ events: [ev(1)], nextAfterSeq: null }));
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    renderHook(() => useLocalSessionHistory(client, "session-1", 1, 0, onAccessLost));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
  });

  it("stops polling when pollMs flips to 0, e.g. the session goes idle", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn(async () => ({ events: [ev(1)], nextAfterSeq: null }));
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { rerender } = renderHook(
      ({ pollMs }) => useLocalSessionHistory(client, "session-1", 1, 0, onAccessLost, { pollMs }),
      { initialProps: { pollMs: 5000 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2);
    rerender({ pollMs: 0 });
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2); // idle: hints plus the safety poll only
  });
});

describe("useLocalSessionHistory scrollback", () => {
  it("reaches back on its own when the latest page draws nothing, and keeps what it scrolled through", async () => {
    vi.useFakeTimers();
    const hidden = (seq: number): LocalSessionEvent => ({ seq, kind: "text", role: "assistant", text: "sub", truncated: false, subagent: "a1" } as LocalSessionEvent);
    // The latest page is all a subagent's rows (a subagent drawn inline needs its launch
    // to be placed at its first event, so use rows that belong to no placeable subagent).
    const invisible = (seq: number): LocalSessionEvent => ({ seq, kind: "lifecycle", role: "system", text: "", lifecycle: "turn_end", truncated: false });
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [invisible(101), invisible(150)], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [invisible(60)], nextAfterSeq: 100 })
      .mockResolvedValueOnce({ events: [ev(10), hidden(11)], nextAfterSeq: 59 });
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result } = renderHook(() => useLocalSessionHistory(client, "session-1", 150, 0, onAccessLost));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3);
    expect(result.current.events.map(e => e.seq)).toEqual([10, 11, 60, 101, 150]);
  });
});

describe("useLocalSessionHistory follows the tail", () => {
  const page = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => ev(from + i));

  it("reads on past a full page until it has the end, and never reports newer activity meanwhile", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: 100 }) // the row was stale: more exists
      .mockResolvedValueOnce({ events: page(101, 150), nextAfterSeq: 150 })
      .mockResolvedValueOnce({ events: page(151, 160), nextAfterSeq: null });
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result } = renderHook(() => useLocalSessionHistory(client, "session-1", 100, 0, onAccessLost));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3);
    expect(listLocalSessionEvents).toHaveBeenNthCalledWith(2, "session-1", 100, 50);
    expect(listLocalSessionEvents).toHaveBeenNthCalledWith(3, "session-1", 150, 50);
    expect(result.current.events.at(-1)?.seq).toBe(160);
    expect(result.current.hasNewer).toBe(false);
  });

  it("fetches when the session row moves ahead of the window, with no hint and no poll", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [ev(4), ev(5)], nextAfterSeq: null });
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ latest }) => useLocalSessionHistory(client, "session-1", latest, 0, onAccessLost),
      { initialProps: { latest: 3 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    rerender({ latest: 5 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 3, 50);
    expect(result.current.events).toHaveLength(5);
    expect(result.current.hasNewer).toBe(false);
  });

  it("keeps following after the reader pages back through older history", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: null }) // latest
      .mockResolvedValueOnce({ events: page(1, 50), nextAfterSeq: 50 }) // older
      .mockResolvedValueOnce({ events: [ev(101)], nextAfterSeq: null }); // the next hint
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 100, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await result.current.load("older"); });
    expect(result.current.events[0].seq).toBe(1);
    expect(result.current.hasNewer).toBe(false);

    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 100, 50);
    expect(result.current.events.at(-1)?.seq).toBe(101);
  });

  // The screen pages back on its own to fill itself, and a foreground load
  // supersedes a quiet one. With a stale row and no hint, nothing else would
  // ever ask for the tail that was dropped.
  it("reads the tail again when an older page superseded the read that was fetching it", async () => {
    vi.useFakeTimers();
    let answerTail: (page: unknown) => void = () => {};
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: 100 }) // latest: a full page, more exists
      .mockImplementationOnce(() => new Promise((resolve) => { answerTail = resolve; })) // the tail read, still in flight
      .mockResolvedValueOnce({ events: page(1, 50), nextAfterSeq: 50 }) // older, which supersedes it
      .mockResolvedValueOnce({ events: page(101, 110), nextAfterSeq: null }); // the tail, asked for again
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result } = renderHook(() => useLocalSessionHistory(client, "session-1", 100, 0, onAccessLost));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await result.current.load("older"); });
    await act(async () => { answerTail({ events: page(101, 110), nextAfterSeq: null }); await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(4);
    expect(result.current.events[0].seq).toBe(1);
    expect(result.current.events.at(-1)?.seq).toBe(110);
    expect(result.current.hasNewer).toBe(false);
  });

  it("keeps the reader's error through background reads, and retries what the reader asked for", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: null }) // latest
      .mockRejectedValueOnce(new Error("offline")) // the reader pages back; it fails
      .mockResolvedValueOnce({ events: [ev(101)], nextAfterSeq: null }) // a hint's background read lands
      .mockResolvedValueOnce({ events: page(1, 50), nextAfterSeq: 50 }); // the retry: older, not the hint again
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 100, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await result.current.load("older"); });
    expect(result.current.error).toBe("Could not load session history. Try again.");

    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.events.at(-1)?.seq).toBe(101);
    expect(result.current.error).toBe("Could not load session history. Try again.");

    await act(async () => { await result.current.retry(); });
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 0, 50);
    expect(result.current.error).toBeNull();
    expect(result.current.events[0].seq).toBe(1);
  });

  it("retries the background read that failed, not the page the reader last asked for", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: null }) // latest
      .mockResolvedValueOnce({ events: page(1, 50), nextAfterSeq: 50 }) // the reader pages back; it lands
      .mockRejectedValueOnce(new Error("offline")) // then a hint's read of the tail fails
      .mockResolvedValueOnce({ events: [ev(101)], nextAfterSeq: null }); // Retry: the tail
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 100, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await result.current.load("older"); });
    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.error).toBe("Could not load session history. Try again.");

    await act(async () => { await result.current.retry(); });
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 100, 50);
    expect(result.current.events.at(-1)?.seq).toBe(101);
    expect(result.current.error).toBeNull();
  });

  it("clears a background read's error when the next one lands", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: null }) // latest
      .mockRejectedValueOnce(new Error("offline")) // a hint's read fails
      .mockResolvedValueOnce({ events: [ev(101)], nextAfterSeq: null }); // the next one lands
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 100, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.error).toBe("Could not load session history. Try again.");
    rerender({ revision: 2 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.error).toBeNull();
    expect(result.current.events.at(-1)?.seq).toBe(101);
  });

  it("reads the tail after an older page when a hint arrived while that page was loading", async () => {
    vi.useFakeTimers();
    let answerOlder: (page: unknown) => void = () => {};
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: null }) // latest
      .mockImplementationOnce(() => new Promise((resolve) => { answerOlder = resolve; })) // older, slow
      .mockResolvedValueOnce({ events: [ev(101)], nextAfterSeq: null }); // the tail the hint asked for
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 100, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    let older: Promise<void> = Promise.resolve();
    act(() => { older = result.current.load("older"); });
    // The session's last output lands now, and with it the only hint there will be.
    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2);

    await act(async () => { answerOlder({ events: page(1, 50), nextAfterSeq: 50 }); await older; await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 100, 50);
    expect(result.current.events[0].seq).toBe(1);
    expect(result.current.events.at(-1)?.seq).toBe(101);
  });

  it("reads the tail again when a second hint arrived while the first read was in flight", async () => {
    vi.useFakeTimers();
    let answerFirst: (page: unknown) => void = () => {};
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: null }) // latest
      .mockImplementationOnce(() => new Promise((resolve) => { answerFirst = resolve; })) // first hint's read, asked early
      .mockResolvedValueOnce({ events: [ev(102)], nextAfterSeq: null }); // what the second hint was about
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "session-1", 100, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    rerender({ revision: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    rerender({ revision: 2 }); // turned away: the first is still in flight
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2);

    await act(async () => { answerFirst({ events: [ev(101)], nextAfterSeq: null }); await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenLastCalledWith("session-1", 101, 50);
    expect(result.current.events.at(-1)?.seq).toBe(102);
  });

  it("leaves a failed older page's error up, and reads the owed tail once the retry lands", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: page(51, 100), nextAfterSeq: 100 }) // latest: a full page, more exists
      .mockImplementationOnce(() => new Promise(() => {})) // the tail read, superseded before it answers
      .mockRejectedValueOnce(new Error("offline")) // older fails
      .mockResolvedValueOnce({ events: page(1, 50), nextAfterSeq: 50 }) // the retry
      .mockResolvedValueOnce({ events: page(101, 110), nextAfterSeq: null }); // the tail, made good after it
    const client = { listLocalSessionEvents } as unknown as SessionSource["client"];
    const onAccessLost = () => {};
    const { result } = renderHook(() => useLocalSessionHistory(client, "session-1", 100, 0, onAccessLost));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await result.current.load("older"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.error).toBe("Could not load session history. Try again.");
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3);

    await act(async () => { await result.current.retry(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.error).toBeNull();
    expect(result.current.events[0].seq).toBe(1);
    expect(result.current.events.at(-1)?.seq).toBe(110);
  });
});
