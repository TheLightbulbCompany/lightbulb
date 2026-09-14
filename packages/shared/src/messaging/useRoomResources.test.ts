// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
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
