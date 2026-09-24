// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useLocalSessionHistory, useRoomResources, type RoomResourceSource } from "./useRoomResources";
import type { LocalSessionEvent } from "./types";

const ev = (seq: number): LocalSessionEvent => ({ seq, kind: "text", role: "assistant", text: `e${seq}`, truncated: false });

const listResources = vi.fn(async () => ({ devices: [], sessions: [], pairedNodes: [] }));
// Module-level so the source is referentially stable across rerenders.
const source = {
  client: { listResources, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
  onEvent: () => () => {},
  connectionRevision: 1,
} as unknown as RoomResourceSource;

afterEach(() => { cleanup(); vi.useRealTimers(); listResources.mockClear(); });

describe("useRoomResources enabled gate", () => {
  it("neither fetches nor polls while disabled, then fetches once enabled, refetches nothing before the 30s safety poll, and refetches once at 30s", async () => {
    vi.useFakeTimers();
    const { rerender } = renderHook(({ enabled }) => useRoomResources("room-1", source, { enabled }), { initialProps: { enabled: false } });
    await act(async () => { await vi.advanceTimersByTimeAsync(31000); });
    expect(listResources).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResources).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(29999); });
    expect(listResources).toHaveBeenCalledTimes(1); // no hint, nothing refetches before 30s
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(listResources).toHaveBeenCalledTimes(2); // the safety poll fires exactly at 30s
    rerender({ enabled: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(listResources).toHaveBeenCalledTimes(2);
  });

  it("defaults to enabled", async () => {
    vi.useFakeTimers();
    renderHook(() => useRoomResources("room-1", source));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResources).toHaveBeenCalledTimes(1);
  });

  it("passes includeEnded through to the client and refetches when it flips", async () => {
    const { rerender } = renderHook(({ includeEnded }) => useRoomResources("room-1", source, { includeEnded }), { initialProps: { includeEnded: false } });
    await waitFor(() => expect(source.client.listResources).toHaveBeenCalledWith("room-1", { includeEnded: false }));
    rerender({ includeEnded: true });
    await waitFor(() => expect(source.client.listResources).toHaveBeenCalledWith("room-1", { includeEnded: true }));
  });

  it("a workspace.sessions.changed push refreshes resources and bumps revision with no timer advance, feeding a session history load(\"newer\")", async () => {
    vi.useFakeTimers();
    const handlers = new Set<(event: unknown) => void>();
    const pushListResources = vi.fn(async () => ({ devices: [], sessions: [], pairedNodes: [] }));
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null }) // initial "latest" history load
      .mockResolvedValueOnce({ events: [ev(4)], nextAfterSeq: null }) // the "newer" load the mount's own revision bump feeds
      .mockResolvedValueOnce({ events: [ev(5)], nextAfterSeq: null }); // the "newer" load the push's revision bump feeds
    const pushSource = {
      client: { listResources: pushListResources, listLocalSessionEvents, submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
      onEvent: (handler: (event: unknown) => void) => { handlers.add(handler); return () => handlers.delete(handler); },
      connectionRevision: 1,
    } as unknown as RoomResourceSource;
    const onAccessLost = () => {}; // stable reference: an inline fn would re-identity `load` every render.
    const { result } = renderHook(() => {
      const resources = useRoomResources("room-1", pushSource);
      const history = useLocalSessionHistory(pushSource.client, "room-1", "session-1", 5, resources.revision, onAccessLost);
      return { resources, history };
    });
    // Mount alone already refreshes resources once and bumps revision once, so
    // the history hook's revision effect already ran by the time this settles.
    await act(async () => { await vi.advanceTimersByTimeAsync(0); }); // flush; no polling elapsed
    expect(pushListResources).toHaveBeenCalledTimes(1);
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(2);
    expect(result.current.history.events).toHaveLength(4);

    // The push hint arrives; only microtasks are flushed below, never a timer interval.
    await act(async () => {
      for (const handler of handlers) handler({ type: "workspace.sessions.changed", payload: { humanUserId: "user-1" }, schemaVersion: 1, eventId: "e1", occurredAt: "now" } as never);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(pushListResources).toHaveBeenCalledTimes(2);
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(3);
    expect(result.current.history.events).toHaveLength(5);
  });

  it("uses the latest includeEnded for a do-while re-run coalesced while the toggle flips mid-fetch", async () => {
    // enabled:false keeps the interval-owning effect (whose cleanup invalidates
    // the in-flight epoch on every dep change, including includeEnded) from ever
    // registering, so a rerender here updates includeEndedRef WITHOUT tearing
    // down refreshing/queued/request — isolating the do-while's own coalescing
    // from that effect's unrelated (and already-correct) full-epoch reset.
    let resolveFirst!: (value: { devices: never[]; sessions: never[]; pairedNodes: never[] }) => void;
    const first = new Promise<{ devices: never[]; sessions: never[]; pairedNodes: never[] }>(resolve => { resolveFirst = resolve; });
    const raceListResources = vi.fn()
      .mockReturnValueOnce(first)
      .mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
    const raceSource = {
      client: { listResources: raceListResources, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
      onEvent: () => () => {},
      connectionRevision: 1,
    } as unknown as RoomResourceSource;

    const { result, rerender } = renderHook(
      ({ includeEnded }) => useRoomResources("room-1", raceSource, { enabled: false, includeEnded }),
      { initialProps: { enabled: false, includeEnded: false } },
    );
    expect(raceListResources).not.toHaveBeenCalled();
    await act(async () => { void result.current.refresh(); }); // enters the do-while, includeEnded: false, pending on `first`
    expect(raceListResources).toHaveBeenNthCalledWith(1, "room-1", { includeEnded: false });
    await act(async () => { void result.current.refresh(); }); // refreshing.current is true -> coalesced, just queues
    expect(raceListResources).toHaveBeenCalledTimes(1);

    rerender({ enabled: false, includeEnded: true }); // updates includeEndedRef; no effect to tear down
    await act(async () => { resolveFirst({ devices: [], sessions: [], pairedNodes: [] }); await first; });
    // The coalesced re-run must read the CURRENT includeEnded, not the value
    // captured by the closure that originally entered the do-while.
    await waitFor(() => expect(raceListResources).toHaveBeenCalledTimes(2));
    expect(raceListResources).toHaveBeenNthCalledWith(2, "room-1", { includeEnded: true });
  });
});

/** A source whose `onEvent` handlers can be driven directly, to fire
 *  `workspace.sessions.changed` hints on demand. */
function hintSource() {
  const handlers = new Set<(event: unknown) => void>();
  const listResourcesHint = vi.fn(async () => ({ devices: [], sessions: [], pairedNodes: [] }));
  const source = {
    client: { listResources: listResourcesHint, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
    onEvent: (handler: (event: unknown) => void) => { handlers.add(handler); return () => handlers.delete(handler); },
    connectionRevision: 1,
  } as unknown as RoomResourceSource;
  const fire = () => {
    for (const handler of handlers) handler({ type: "workspace.sessions.changed", payload: { humanUserId: "user-1" }, schemaVersion: 1, eventId: "e", occurredAt: "now" } as never);
  };
  return { source, listResourcesHint, fire };
}

describe("useRoomResources trailing hint refresh", () => {
  it("a hint gives one immediate refresh plus exactly one trailing refresh at +1500ms, then nothing until the 30s poll", async () => {
    vi.useFakeTimers();
    const { source, listResourcesHint, fire } = hintSource();
    renderHook(() => useRoomResources("room-1", source));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); }); // the mount fetch settles
    expect(listResourcesHint).toHaveBeenCalledTimes(1);

    await act(async () => { fire(); await vi.advanceTimersByTimeAsync(0); }); // the hint's immediate refresh
    expect(listResourcesHint).toHaveBeenCalledTimes(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(1499); });
    expect(listResourcesHint).toHaveBeenCalledTimes(2); // the trailing refresh is not due yet
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(listResourcesHint).toHaveBeenCalledTimes(3); // exactly one trailing refresh, at +1500ms

    // Nothing more until the 30s safety poll, which started ticking at mount
    // and is unaffected by the hint; 1500ms of the 30s window already elapsed above.
    await act(async () => { await vi.advanceTimersByTimeAsync(30000 - 1500 - 1); });
    expect(listResourcesHint).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(listResourcesHint).toHaveBeenCalledTimes(4); // the 30s safety poll
  });

  it("three hints 400ms apart give three immediate refreshes (coalescing as today) plus exactly one trailing refresh 1500ms after the last hint", async () => {
    vi.useFakeTimers();
    const { source, listResourcesHint, fire } = hintSource();
    renderHook(() => useRoomResources("room-1", source));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResourcesHint).toHaveBeenCalledTimes(1);

    await act(async () => { fire(); await vi.advanceTimersByTimeAsync(0); }); // hint 1
    expect(listResourcesHint).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(400); fire(); await vi.advanceTimersByTimeAsync(0); }); // hint 2, resets the trailing timer
    expect(listResourcesHint).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(400); fire(); await vi.advanceTimersByTimeAsync(0); }); // hint 3, resets it again
    expect(listResourcesHint).toHaveBeenCalledTimes(4);

    // Only 1499ms have passed since the last (3rd) hint -- no trailing refresh yet.
    await act(async () => { await vi.advanceTimersByTimeAsync(1499); });
    expect(listResourcesHint).toHaveBeenCalledTimes(4);
    // Exactly one trailing refresh fires 1500ms after the LAST hint -- the
    // earlier hints' timers were reset, not left to fire their own.
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(listResourcesHint).toHaveBeenCalledTimes(5);
  });

  it("unmounting before the trailing timer fires means no refresh", async () => {
    vi.useFakeTimers();
    const { source, listResourcesHint, fire } = hintSource();
    const { unmount } = renderHook(() => useRoomResources("room-1", source));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResourcesHint).toHaveBeenCalledTimes(1);

    await act(async () => { fire(); await vi.advanceTimersByTimeAsync(0); }); // schedules the trailing timer
    expect(listResourcesHint).toHaveBeenCalledTimes(2);

    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); }); // well past +1500ms
    expect(listResourcesHint).toHaveBeenCalledTimes(2); // the trailing timer never fired
  });
});

describe("useLocalSessionHistory tail stability", () => {
  it("keeps the same events array across an empty poll, and hands out a new one when an event arrives", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [], nextAfterSeq: null })
      .mockResolvedValueOnce({ events: [ev(4)], nextAfterSeq: null });
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {}; // stable reference: an inline fn would re-identity `load` every render.
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "room-1", "session-1", 3, revision, onAccessLost),
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
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "room-1", "session-1", 3, revision, onAccessLost),
      { initialProps: { revision: 0 } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toHaveLength(3);

    // The revision effect, driven by useRoomResources' push hint or its 30s safety poll.
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
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    const { result } = renderHook(({ revision }) => useLocalSessionHistory(client, "room-1", "session-1", 3, revision, onAccessLost), { initialProps: { revision: 0 } });
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
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision }) => useLocalSessionHistory(client, "room-1", "session-1", 50, revision, onAccessLost),
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
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    const { result, unmount } = renderHook(
      () => useLocalSessionHistory(client, "room-1", "session-1", 2, 0, onAccessLost, { pollMs: 5000 }),
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
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    renderHook(() => useLocalSessionHistory(client, "room-1", "session-1", 1, 0, onAccessLost));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(listLocalSessionEvents).toHaveBeenCalledTimes(1);
  });

  it("stops polling when pollMs flips to 0, e.g. the session goes idle", async () => {
    vi.useFakeTimers();
    const listLocalSessionEvents = vi.fn(async () => ({ events: [ev(1)], nextAfterSeq: null }));
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    const { rerender } = renderHook(
      ({ pollMs }) => useLocalSessionHistory(client, "room-1", "session-1", 1, 0, onAccessLost, { pollMs }),
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

describe("useRoomResources transcript hints", () => {
  it("refetches for a transcript hint only when its sharer has a session in this pane", async () => {
    const handlers = new Set<(event: unknown) => void>();
    const listResources = vi.fn(async () => ({ devices: [], sessions: [{ sessionId: "s1", humanUserId: "alice" }], pairedNodes: [] }));
    const source = {
      client: { listResources, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
      onEvent: (handler: (event: unknown) => void) => { handlers.add(handler); return () => handlers.delete(handler); },
      connectionRevision: 1,
    } as unknown as RoomResourceSource;
    const fire = (humanUserId: string) => {
      for (const handler of handlers) handler({ type: "workspace.sessions.changed", payload: { humanUserId, transcript: true }, schemaVersion: 1, eventId: humanUserId, occurredAt: "now" } as never);
    };
    renderHook(() => useRoomResources("room-1", source));
    await waitFor(() => expect(listResources).toHaveBeenCalledTimes(1));
    act(() => fire("bob")); // streaming in someone else's session: not this pane's business
    expect(listResources).toHaveBeenCalledTimes(1);
    act(() => fire("alice"));
    await waitFor(() => expect(listResources).toHaveBeenCalledTimes(2));
  });
});
