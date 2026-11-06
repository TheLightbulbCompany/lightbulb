// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ASK_AGAIN_AFTER_MS, useSessionJoinRequest, watchOnlyLine } from "./sessionControls";
import type { LocalSession } from "./types";

afterEach(() => { cleanup(); vi.useRealTimers(); });
const session = (c: Partial<LocalSession> = {}) => ({ sessionId: "s1", status: "busy", ...c }) as LocalSession;

const declined = session({ pickupRequest: "declined" });
const plain = session();

describe("useSessionJoinRequest kind", () => {
  it("asks to pick up through the pick-up door and reflects pickupRequest", async () => {
    const pick = vi.fn().mockResolvedValue(session({ pickupRequest: "pending" }));
    const join = vi.fn();
    const client = { submitLocalSessionCommand: vi.fn(), requestLocalSessionPickupAccess: pick, requestLocalSessionJoin: join };
    const { result } = renderHook(() => useSessionJoinRequest(client, declined, "pickup"));
    expect(result.current.state).toBe("declined");
    await act(async () => { await result.current.ask(); });
    expect(pick).toHaveBeenCalledWith("s1");
    expect(join).not.toHaveBeenCalled();
    expect(result.current.state).toBe("pending");
  });

  it("stays on the join door by default", async () => {
    const join = vi.fn().mockResolvedValue(session({ joinRequest: "pending" }));
    const client = { submitLocalSessionCommand: vi.fn(), requestLocalSessionJoin: join };
    const { result } = renderHook(() => useSessionJoinRequest(client, plain));
    await act(async () => { await result.current.ask(); });
    expect(join).toHaveBeenCalledWith("s1");
    expect(result.current.state).toBe("pending");
  });
});

// ISOA-729: a request never times out, so the person can remind the owner (Ask to pick up).
describe("an unanswered request", () => {
  const client = (pick = vi.fn()) => ({ submitLocalSessionCommand: vi.fn(), requestLocalSessionPickupAccess: pick });

  it("reads as waiting, then offers Ask again after the window, and asking again sends and waits anew", async () => {
    vi.useFakeTimers();
    const pending = session({ pickupRequest: "pending", pickupRequestedAt: new Date().toISOString() });
    const pick = vi.fn().mockResolvedValue(pending);
    const { result } = renderHook(() => useSessionJoinRequest(client(pick), pending, "pickup"));
    expect([result.current.state, result.current.again]).toEqual(["pending", false]);
    act(() => { vi.advanceTimersByTime(ASK_AGAIN_AFTER_MS + 1); });
    expect(result.current.again).toBe(true);
    await act(async () => { await result.current.ask(); });
    expect(pick).toHaveBeenCalledWith("s1");
    expect(result.current.again).toBe(false); // not offered again straight after it is used
  });

  it("keeps Ask again offered when the reminder could not be sent", async () => {
    const old = new Date(Date.now() - ASK_AGAIN_AFTER_MS - 1000).toISOString();
    const pick = vi.fn().mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useSessionJoinRequest(client(pick), session({ pickupRequest: "pending", pickupRequestedAt: old }), "pickup"));
    expect(result.current.again).toBe(true);
    await act(async () => { await result.current.ask(); });
    expect(result.current.error).toBe("Could not send that. Try again.");
    expect(result.current.again).toBe(true);
  });

  it("never offers Ask again against a backend that does not say when the request was made", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useSessionJoinRequest(client(), session({ pickupRequest: "pending" }), "pickup"));
    act(() => { vi.advanceTimersByTime(ASK_AGAIN_AFTER_MS * 2); });
    expect(result.current.again).toBe(false);
  });
});

it("a teammate who cannot type is told who can, with nothing to ask (owner ruling 2026-10-26)", () => {
  expect(watchOnlyLine("Ada")).toBe("Only Ada can type in this session.");
});
