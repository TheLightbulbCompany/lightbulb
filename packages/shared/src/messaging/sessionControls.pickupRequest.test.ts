// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ASK_AGAIN_AFTER_MS, joinRequestView, useSessionJoinRequest } from "./sessionControls";
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

// ISOA-729: a request never times out, so its lifecycle is said out loud.
describe("an unanswered request", () => {
  const client = (join = vi.fn()) => ({ submitLocalSessionCommand: vi.fn(), requestLocalSessionJoin: join });

  it("reads as waiting, then offers Ask again after the window, and asking again sends and waits anew", async () => {
    vi.useFakeTimers();
    const asked = new Date().toISOString();
    const pending = session({ joinRequest: "pending", joinRequestedAt: asked });
    const join = vi.fn().mockResolvedValue(pending);
    const { result } = renderHook(() => useSessionJoinRequest(client(join), pending));
    expect(joinRequestView(result.current, "Ada")).toEqual({ line: "Waiting for Ada to answer. Your request stays open until they do or the session ends.", button: "Requested", disabled: true });
    act(() => { vi.advanceTimersByTime(ASK_AGAIN_AFTER_MS + 1); });
    expect(joinRequestView(result.current, "Ada")).toEqual({ line: "Ada has not answered your request yet.", button: "Ask again", disabled: false });
    await act(async () => { await result.current.ask(); });
    expect(join).toHaveBeenCalledWith("s1");
    expect(joinRequestView(result.current, "Ada").button).toBe("Requested"); // not offered again straight after it is used
  });

  it("keeps Ask again offered when the reminder could not be sent", async () => {
    const old = new Date(Date.now() - ASK_AGAIN_AFTER_MS - 1000).toISOString();
    const join = vi.fn().mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useSessionJoinRequest(client(join), session({ joinRequest: "pending", joinRequestedAt: old })));
    expect(result.current.again).toBe(true);
    await act(async () => { await result.current.ask(); });
    expect(result.current.error).toBe("Could not send your request. Try again.");
    expect(joinRequestView(result.current, "Ada")).toEqual({ line: "Ada has not answered your request yet.", button: "Ask again", disabled: false });
  });

  it("never offers Ask again against a backend that does not say when the request was made", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useSessionJoinRequest(client(), session({ joinRequest: "pending" })));
    act(() => { vi.advanceTimersByTime(ASK_AGAIN_AFTER_MS * 2); });
    expect(result.current.again).toBe(false);
  });

  it("says so when the server reports the request closed by a restart, on a fresh mount, until the person asks again", async () => {
    // Both session views remount on a new generation: the hook starts with no memory of "pending".
    const join = vi.fn().mockResolvedValue(session({ joinRequest: "pending" }));
    const closed = session({ joinRequestClosed: "restarted" });
    const { result } = renderHook(() => useSessionJoinRequest(client(join), closed));
    expect(joinRequestView(result.current, "Ada")).toEqual({ line: "Your request closed before Ada answered, because the session restarted.", button: "Ask again", disabled: false });
    await act(async () => { await result.current.ask(); });
    expect(result.current.lapsed).toBe(false);
    expect(joinRequestView(result.current, "Ada").button).toBe("Requested");
  });

  it("does not call a failed first ask a lapse, and keeps the declined and not-asked lines", async () => {
    const join = vi.fn().mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useSessionJoinRequest(client(join), plain));
    await act(async () => { await result.current.ask(); });
    expect(result.current.lapsed).toBe(false);
    expect(joinRequestView(result.current, "Ada")).toEqual({ line: "Only Ada can send to this session.", button: "Ask to join", disabled: false });
    expect(joinRequestView({ state: "declined", again: false, lapsed: false, saving: false }, "Ada").line).toBe("Ada declined your request.");
  });
});
