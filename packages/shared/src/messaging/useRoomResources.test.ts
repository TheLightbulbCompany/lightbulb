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
});
