import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";

const h = vi.hoisted(() => ({ handlers: new Set<(e: LiveEvent) => void>(), get: vi.fn() }));
const api = vi.hoisted(() => ({ get: undefined as unknown }));
vi.mock("@/lib/api", () => { h.get.mockResolvedValue({ calls: [{ callId: "c1", userId: "user_b" }] }); api.get = { get: h.get }; return { useApi: () => api.get }; });
vi.mock("@/hooks/useLiveEvents", () => {
  const onEvent = (fn: (e: LiveEvent) => void) => { h.handlers.add(fn); return () => h.handlers.delete(fn); };
  return { useLiveEvents: () => ({ onEvent }) };
});
import { useRoomCallers } from "../useRoomCallers";

const emit = (roomId: string, userId: string, state: "started" | "ended") =>
  act(() => { h.handlers.forEach((fn) => fn({ type: "room.call.changed", payload: { roomId, callId: "c", userId, state } } as LiveEvent)); });

it("seeds from the server, then follows started/ended for this room only", async () => {
  const { result } = renderHook(() => useRoomCallers("r1"));
  await waitFor(() => expect(result.current).toEqual(["user_b"]));
  expect(h.get).toHaveBeenCalledWith("/calls/active?roomId=r1");
  emit("r2", "user_x", "started");
  expect(result.current).toEqual(["user_b"]);
  emit("r1", "user_c", "started");
  expect(result.current).toEqual(["user_b", "user_c"]);
  emit("r1", "user_b", "ended");
  expect(result.current).toEqual(["user_c"]);
});
