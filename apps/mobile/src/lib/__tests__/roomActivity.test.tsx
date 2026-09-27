import { act, renderHook } from "@testing-library/react-native";

const mockHandlers = new Set<(event: unknown) => void>();
const mockLive = { onEvent: (h: (event: unknown) => void) => { mockHandlers.add(h); return () => mockHandlers.delete(h); }, connectionRevision: 1 };
jest.mock("../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import { useRoomActivity } from "../roomActivity";

beforeEach(() => mockHandlers.clear());

it("turns user-channel hints into a row's live line", async () => {
  const { result } = await renderHook(() => useRoomActivity());
  await act(async () => {
    for (const h of mockHandlers) {
      h({ type: "room.activity.changed", payload: { roomId: "r1", actor: { kind: "agent", id: "ceo", displayName: "CEO" }, state: "working" } });
    }
  });
  expect(result.current.liveLine("r1", "dm")).toBe("working…");
  expect(result.current.working("r1", "ceo")).toBe(true);
});

it("clears a working entry once its expiresAt passes, on a tick", async () => {
  jest.useFakeTimers();
  const { result } = await renderHook(() => useRoomActivity());
  await act(async () => {
    for (const h of mockHandlers) {
      h({
        type: "room.activity.changed",
        payload: { roomId: "r1", actor: { kind: "agent", id: "ceo", displayName: "CEO" }, state: "working", expiresAt: new Date(Date.now() + 500).toISOString() },
      });
    }
  });
  expect(result.current.working("r1", "ceo")).toBe(true);
  await act(async () => {
    jest.advanceTimersByTime(1_500);
  });
  expect(result.current.working("r1", "ceo")).toBe(false);
  expect(result.current.liveLine("r1", "dm")).toBeNull();
  jest.useRealTimers();
});
