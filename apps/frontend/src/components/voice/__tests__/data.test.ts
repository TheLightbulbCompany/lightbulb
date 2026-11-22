import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ isLoaded: true, userId: "user_a" as string | null }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth })); // no orgId: a person's session has none
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import { callNeedsYou, callSummary, mmss, nameOf, useProvisionOnOpen, useTextChats, useUsage } from "../data";

beforeEach(() => { Object.assign(auth, { isLoaded: true, userId: "user_a" }); api.get.mockReset(); api.post.mockReset().mockResolvedValue({}); });

describe("test_scopes_keyed_on_user", () => {
  it("lists a person's chats and usage with no organization in the session", async () => {
    api.get.mockImplementation(async (path: string) => path === "/messaging/rooms"
      ? { rooms: [{ roomId: "dm", kind: "dm", members: [{ kind: "agent", id: "a" }, { kind: "human", id: "user_a" }], updatedAt: "2026-11-20T00:00:00Z" }] }
      : { used: 1.4, included: 1500, overageRate: 0.35, runOut: "keep" });
    const chats = renderHook(() => useTextChats());
    renderHook(() => useUsage());
    await waitFor(() => expect(chats.result.current.data?.map((c) => c.roomId)).toEqual(["dm"]));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/voice/usage"));
  });
});

describe("useProvisionOnOpen", () => {
  it("provisions a person who has no workspace yet", async () => {
    api.get.mockResolvedValue({ container_status: null });
    renderHook(() => useProvisionOnOpen());
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/container/provision", {}));
    expect(api.get).toHaveBeenCalledWith("/container/status");
  });
  it("retries a failed workspace once and leaves a running one alone", async () => {
    api.get.mockResolvedValue({ container_status: "error" });
    const { rerender } = renderHook(() => useProvisionOnOpen());
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/container/retry", {}));
    rerender();
    expect(api.get).toHaveBeenCalledTimes(1);
    api.post.mockClear();
    api.get.mockResolvedValue({ container_status: "running" });
    auth.userId = "user_b";
    rerender();
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(api.post).not.toHaveBeenCalled();
  });
  it("does nothing before anyone is signed in, and nothing more when the trial is over (402)", async () => {
    auth.userId = null;
    const { rerender } = renderHook(() => useProvisionOnOpen());
    expect(api.get).not.toHaveBeenCalled();
    auth.userId = "user_a";
    api.get.mockRejectedValue(Object.assign(new Error("blocked"), { status: 402 }));
    rerender();
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    expect(api.post).not.toHaveBeenCalled();
  });
});

describe("voice data helpers", () => {
  it("a call needs you until it is resolved, and only for the three waiting outcomes", () => {
    const call = { type: "call" as const, roomId: "r", at: 0 };
    expect(callNeedsYou({ ...call, outcome: "needs_you" })).toBe(true);
    expect(callNeedsYou({ ...call, outcome: "handoff_missed" })).toBe(true);
    expect(callNeedsYou({ ...call, outcome: "message" })).toBe(true); // a taken message: the owner calls back
    expect(callNeedsYou({ ...call, outcome: "needs_you", resolvedAt: "2026-11-07" })).toBe(false);
    expect(callNeedsYou({ ...call, outcome: "booked" })).toBe(false);
    expect(callNeedsYou({ type: "meeting", roomId: "m", at: 0 })).toBe(false);
  });
  it("formats durations and falls back to Lightbulb for an unnamed assistant", () => {
    expect(mmss(160)).toBe("2:40");
    expect(mmss(9)).toBe("0:09");
    expect(mmss(undefined)).toBe("");
    expect(nameOf({ name: "  " })).toBe("Lightbulb");
    expect(nameOf({ name: "Nova" })).toBe("Nova");
  });
});

describe("monthMinutes", () => {
  it("sums this month's call seconds to whole minutes", async () => {
    const { monthMinutes } = await import("../data");
    const now = new Date(2026, 10, 7, 12);
    const c = (at: Date, durationSec: number) => ({ type: "call" as const, roomId: "r", at: at.getTime(), durationSec });
    expect(monthMinutes([c(new Date(2026, 10, 2), 1200), c(new Date(2026, 10, 5), 1080), c(new Date(2026, 9, 30), 6000)], now)).toBe(38);
  });
});

it("uses the latest recorded non-guessed step when a call summary is empty", () => {
  const steps = [{ text: "Booked Tuesday at 10" }, { text: "Maybe completed", guessed: true }];
  expect(callSummary({ steps })).toBe("Booked Tuesday at 10");
  expect(callSummary({ summary: "Confirmed with caller", steps })).toBe("Confirmed with caller");
  expect(callSummary({ steps: [{ text: "Maybe completed", guessed: true }] })).toBe("Open conversation");
});
