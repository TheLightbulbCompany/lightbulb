// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useLocalSessionHistory, usePolledSnapshot, useRoomResources, type RoomResourceSource } from "./useRoomResources";
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

describe("usePolledSnapshot", () => {
  // enabled: false, so only the explicit refresh() calls below fetch.
  const options = { enabled: false, intervalMs: 5000, isHint: () => false };

  it("turns two refreshes during an in-flight fetch into exactly one follow-up fetch", async () => {
    const pending: Array<() => void> = [];
    const fetch = vi.fn(() => new Promise<void>(resolve => pending.push(resolve)));
    const load = async () => { await fetch(); return () => {}; };
    const { result } = renderHook(() => usePolledSnapshot(load, source, options));
    act(() => { void result.current.refresh(); });
    act(() => { void result.current.refresh(); void result.current.refresh(); });
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => pending[0]());
    expect(fetch).toHaveBeenCalledTimes(2);
    await act(async () => pending[1]());
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
  });

  it("discards a response that started before invalidate(), and it cannot settle the new epoch", async () => {
    const pending: Array<(value: string) => void> = [];
    const applied: string[] = [];
    const load = async () => {
      const value = await new Promise<string>(resolve => pending.push(resolve));
      return () => { applied.push(value); };
    };
    const { result } = renderHook(() => usePolledSnapshot(load, source, options));
    act(() => { void result.current.refresh(); });
    act(() => { result.current.invalidate(); void result.current.refresh(); });
    await act(async () => pending[0]("old epoch"));
    expect(applied).toEqual([]);
    expect(result.current.loading).toBe(true); // the new epoch's fetch is still out
    await act(async () => pending[1]("new epoch"));
    expect(applied).toEqual(["new epoch"]);
    expect(result.current.loading).toBe(false);
  });
});

describe("useRoomResources enabled gate", () => {
  it("neither fetches nor polls while disabled, then fetches once enabled and polls every 5s", async () => {
    vi.useFakeTimers();
    const { rerender } = renderHook(({ enabled }) => useRoomResources("room-1", source, { enabled }), { initialProps: { enabled: false } });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(listResources).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResources).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(listResources).toHaveBeenCalledTimes(2);
    rerender({ enabled: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
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

  it("never flashes loading on the two background callers (revision bump, busy tail), and new events still arrive", async () => {
    vi.useFakeTimers();
    let resolveRevision!: (v: { events: LocalSessionEvent[]; nextAfterSeq: number | null }) => void;
    let resolveTail!: (v: { events: LocalSessionEvent[]; nextAfterSeq: number | null }) => void;
    const revisionFetch = new Promise<{ events: LocalSessionEvent[]; nextAfterSeq: number | null }>(resolve => { resolveRevision = resolve; });
    const tailFetch = new Promise<{ events: LocalSessionEvent[]; nextAfterSeq: number | null }>(resolve => { resolveTail = resolve; });
    const listLocalSessionEvents = vi.fn()
      .mockResolvedValueOnce({ events: [ev(1), ev(2), ev(3)], nextAfterSeq: null })
      .mockReturnValueOnce(revisionFetch)
      .mockReturnValueOnce(tailFetch);
    const client = { listLocalSessionEvents } as unknown as RoomResourceSource["client"];
    const onAccessLost = () => {};
    const { result, rerender } = renderHook(
      ({ revision, busy }) => useLocalSessionHistory(client, "room-1", "session-1", 3, revision, onAccessLost, { busy }),
      { initialProps: { revision: 0, busy: false } },
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toHaveLength(3);

    // Background caller 1: the revision effect, driven by useRoomResources' 5s poll.
    rerender({ revision: 1, busy: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.loading).toBe(false); // stayed false while the fetch is in flight
    await act(async () => { resolveRevision({ events: [ev(4)], nextAfterSeq: null }); await revisionFetch; });
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toHaveLength(4);

    // Background caller 2: the 1s busy tail.
    rerender({ revision: 1, busy: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.loading).toBe(false); // stayed false while the fetch is in flight
    await act(async () => { resolveTail({ events: [ev(5)], nextAfterSeq: null }); await tailFetch; });
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toHaveLength(5);
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
});
