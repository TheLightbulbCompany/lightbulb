import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";

const h = vi.hoisted(() => ({ handlers: new Set<(e: LiveEvent) => void>(), get: vi.fn(), rev: 0, client: {} as { get: unknown } }));
vi.mock("@/lib/api", () => { h.client = { get: h.get }; return { useApi: () => h.client }; });
vi.mock("@/hooks/useLiveEvents", () => {
  const onEvent = (fn: (e: LiveEvent) => void) => { h.handlers.add(fn); return () => h.handlers.delete(fn); };
  return { useLiveEvents: () => ({ onEvent, connectionRevision: h.rev }) };
});
import { useRoomHuddle } from "../useRoomHuddle";

const emit = (roomId: string, userIds: string[]) =>
  act(() => { h.handlers.forEach((fn) => fn({ type: "room.huddle.changed", payload: { roomId, userIds } } as LiveEvent)); });

beforeEach(() => { h.handlers.clear(); h.rev = 0; h.get.mockReset(); h.get.mockResolvedValue({ userIds: ["user_b"] }); });

it("seeds from the server, then follows events for this room only", async () => {
  const { result } = renderHook(() => useRoomHuddle("r1"));
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  expect(h.get).toHaveBeenCalledWith("/huddles/r1");
  emit("r2", ["user_x"]);
  expect(result.current).toEqual(["user_b"]);
  emit("r1", ["user_b", "user_c"]);
  expect(result.current).toEqual(["user_b", "user_c"]);
});

it("does not resurrect a user who left before the seed landed", async () => {
  let resolve!: (v: unknown) => void;
  h.get.mockReturnValue(new Promise((r) => { resolve = r; }));
  const { result } = renderHook(() => useRoomHuddle("r1"));
  emit("r1", []);
  await act(async () => { resolve({ userIds: ["user_b"] }); });
  expect(result.current).toEqual([]);
});

it("re-reads the seed on reconnect so a missed event heals", async () => {
  const { result, rerender } = renderHook(() => useRoomHuddle("r1"));
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  h.get.mockResolvedValue({ userIds: [] });
  h.rev = 1;
  rerender();
  await waitFor(() => expect(result.current).toEqual([]));
});
