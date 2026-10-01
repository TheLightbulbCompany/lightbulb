import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";

const h = vi.hoisted(() => ({ handlers: new Set<(e: LiveEvent) => void>(), get: vi.fn(), rev: 0, client: {} as { get: unknown } }));
vi.mock("@/lib/api", () => { h.client = { get: h.get }; return { useApi: () => h.client }; });
vi.mock("@/hooks/useLiveEvents", () => {
  const onEvent = (fn: (e: LiveEvent) => void) => { h.handlers.add(fn); return () => h.handlers.delete(fn); };
  return { useLiveEvents: () => ({ onEvent, connectionRevision: h.rev }) };
});
import { useRoomCallers } from "../useRoomCallers";

const emit = (roomId: string, userId: string, state: "started" | "ended", callId = "c") =>
  act(() => { h.handlers.forEach((fn) => fn({ type: "room.call.changed", payload: { roomId, callId, userId, state } } as LiveEvent)); });
const seedWith = (...calls: [string, string][]) => ({ calls: calls.map(([callId, userId]) => ({ callId, userId })) });

beforeEach(() => { h.handlers.clear(); h.rev = 0; h.get.mockReset(); h.get.mockResolvedValue(seedWith(["c1", "user_b"])); });

it("seeds from the server, then follows started/ended for this room only", async () => {
  const { result } = renderHook(() => useRoomCallers("r1"));
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  expect(h.get).toHaveBeenCalledWith("/calls/active?roomId=r1");
  emit("r2", "user_x", "started");
  expect(result.current).toEqual(["user_b"]);
  emit("r1", "user_c", "started", "c2");
  expect(result.current).toEqual(["user_b", "user_c"]);
  emit("r1", "user_b", "ended", "c1");
  expect(result.current).toEqual(["user_c"]);
});

it("does not resurrect a call that ended while the seed was in flight", async () => {
  let resolve!: (v: unknown) => void;
  h.get.mockReturnValue(new Promise((r) => { resolve = r; }));
  const { result } = renderHook(() => useRoomCallers("r1"));
  emit("r1", "user_b", "ended", "c1");
  await act(async () => { resolve(seedWith(["c1", "user_b"], ["c9", "user_z"])); });
  expect(result.current).toEqual(["user_z"]);
});

it("keeps a quick redial: ended c1 then started c2 by the same user", async () => {
  let resolve!: (v: unknown) => void;
  h.get.mockReturnValue(new Promise((r) => { resolve = r; }));
  const { result } = renderHook(() => useRoomCallers("r1"));
  emit("r1", "user_b", "ended", "c1");
  emit("r1", "user_b", "started", "c2");
  await act(async () => { resolve(seedWith(["c1", "user_b"])); });
  expect(result.current).toEqual(["user_b"]);
});

it("re-reads the seed on reconnect so a missed ended heals", async () => {
  const { result, rerender } = renderHook(() => useRoomCallers("r1"));
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  h.get.mockResolvedValue(seedWith());
  h.rev = 1;
  rerender();
  await waitFor(() => expect(result.current).toEqual([]));
  expect(h.get).toHaveBeenCalledTimes(2);
});

it("retries a failed seed", async () => {
  vi.useFakeTimers();
  h.get.mockRejectedValueOnce(new Error("x"));
  const { result } = renderHook(() => useRoomCallers("r1"));
  await act(async () => { await vi.advanceTimersByTimeAsync(5100); });
  expect(h.get).toHaveBeenCalledTimes(2);
  expect(result.current).toEqual(["user_b"]);
  vi.useRealTimers();
});

it("switches rooms without leaking the previous room's callers", async () => {
  const { result, rerender } = renderHook(({ id }) => useRoomCallers(id), { initialProps: { id: "r1" } });
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  h.get.mockResolvedValue(seedWith());
  rerender({ id: "r2" });
  expect(result.current).toEqual([]);
  await waitFor(() => expect(h.get).toHaveBeenCalledWith("/calls/active?roomId=r2"));
  emit("r1", "user_q", "started", "cq");
  expect(result.current).toEqual([]);
});

it("shows a user once when started arrives twice", async () => {
  const { result } = renderHook(() => useRoomCallers("r1"));
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  emit("r1", "user_c", "started", "c2");
  emit("r1", "user_c", "started", "c2");
  expect(result.current).toEqual(["user_b", "user_c"]);
});
