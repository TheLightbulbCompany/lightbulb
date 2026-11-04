// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import { canResumeSession, resumeStatus, useSessionResume } from "./sessionControls";
import type { LocalSession } from "./types";

const ended = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.288",
  generation: "g1", status: "ended", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, resumeAllowed: ["bob"], resumable: true, ...changes,
});
const nameOf = (id: string) => (id === "bob" ? "Bob" : "Someone");
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("canResumeSession", () => {
  it("is the owner's and the people who could send when it ended, once, on an ended session", () => {
    expect(canResumeSession(ended(), true)).toBe(true);
    expect(canResumeSession(ended(), false, "bob")).toBe(true);
    expect(canResumeSession(ended(), false, "carol")).toBe(false);
    expect(canResumeSession(ended({ status: "idle" }), true)).toBe(false);
    expect(canResumeSession(ended({ resumable: false }), true)).toBe(false);
    expect(canResumeSession(ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "" } }), true)).toBe(false);
    expect(canResumeSession(ended({ resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "FOLDER_MISSING" } }), false, "bob")).toBe(true);
  });
});

describe("resumeStatus", () => {
  it("says who asked and whose Mac it waits for, or why the Mac refused", () => {
    expect(resumeStatus(ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "" } }), false, "Alice", nameOf, "bob"))
      .toBe("Opening this session on Alice’s Mac…");
    expect(resumeStatus(ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "" } }), true, "Alice", nameOf, "alice"))
      .toBe("Bob asked to open this session on your Mac.");
    expect(resumeStatus(ended({ resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "ALREADY_RUNNING" } }), false, "Alice", nameOf, "bob"))
      .toBe("Not resumed: it is still running on Alice’s Mac, where Lightbulb cannot show it. End it there, then resume.");
    expect(resumeStatus(ended({ resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "CONTINUED" } }), true, "Alice", nameOf, "alice"))
      .toBe("Not resumed: it continues as a newer session on your Mac. Open that session instead.");
    expect(resumeStatus(ended({ cwdName: "lightbulb", resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "FOLDER_NOT_APPROVED" } }), false, "Alice", nameOf, "bob"))
      .toBe("Not resumed: lightbulb is no longer an approved folder on Alice’s Mac.");
    expect(resumeStatus(ended(), false, "Alice", nameOf, "bob")).toBeNull();
  });
});

describe("useSessionResume", () => {
  const opts = { own: false, userId: "bob", ownerName: "Alice", nameOf };

  it("retries a lost reply as the same request and starts a new one after a refusal", async () => {
    let n = 0;
    const resumeLocalSession = vi.fn()
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(ended())
      .mockRejectedValueOnce(new ApiError(409, {}));
    const client = { submitLocalSessionCommand: vi.fn(), resumeLocalSession };
    const { result } = renderHook(() => useSessionResume(client, ended(), { ...opts, uuid: () => `r-${++n}` }));
    await act(async () => { expect(await result.current.resume("go on")).toBe(false); });
    expect(result.current.error).toBe("Could not reach Lightbulb. Try again.");
    await act(async () => { expect(await result.current.resume("go on")).toBe(true); });
    expect(resumeLocalSession.mock.calls.map(c => c[1].resumeId)).toEqual(["r-1", "r-1"]);
    await act(async () => { await result.current.resume(""); });
    expect(resumeLocalSession.mock.calls[2][1]).toEqual({ resumeId: "r-2" });
  });

  it("shows the first message as Delivery unknown when its outcome is lost, and Not delivered when it never ran", async () => {
    const getLocalSessionCommand = vi.fn().mockResolvedValue({ state: "unknown", requestId: "r-1" });
    const client = { submitLocalSessionCommand: vi.fn(), resumeLocalSession: vi.fn(async () => ended()), getLocalSessionCommand };
    const { result, rerender } = renderHook(({ s }) => useSessionResume(client, s, { ...opts, uuid: () => "r-1" }), { initialProps: { s: ended({ sessionId: "s-unknown" }) } });
    await act(async () => { await result.current.resume("go on"); });
    expect(result.current.chip).toBeNull(); // still ended: nothing to report yet
    rerender({ s: ended({ sessionId: "s-unknown", status: "idle", generation: "g2" }) });
    await waitFor(() => expect(result.current.chip?.label).toBe("Delivery unknown"));
    expect(getLocalSessionCommand).toHaveBeenCalledWith("s-unknown", "r-1");

    const missing = { ...client, getLocalSessionCommand: vi.fn().mockRejectedValue(new ApiError(404, {})) };
    const second = renderHook(({ s }) => useSessionResume(missing, s, { ...opts, uuid: () => "r-9" }), { initialProps: { s: ended({ sessionId: "s-missing" }) } });
    await act(async () => { await second.result.current.resume("go on"); });
    second.rerender({ s: ended({ sessionId: "s-missing", status: "idle", generation: "g2" }) });
    await waitFor(() => expect(second.result.current.chip?.label).toBe("Not delivered"));
  });

  it("shows a waiting resume at once, and a Mac's refusal when a later read carries it", async () => {
    const pending = ended({ sessionId: "s-wait", resume: { state: "pending", byUserId: "bob", requestedAt: "" } });
    const refused = ended({ sessionId: "s-wait", resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "FOLDER_MISSING" } });
    const getLocalSession = vi.fn().mockResolvedValue(refused);
    const client = { submitLocalSessionCommand: vi.fn(), resumeLocalSession: vi.fn(async () => pending), getLocalSession };
    vi.useFakeTimers();
    const { result } = renderHook(() => useSessionResume(client, ended({ sessionId: "s-wait" }), { ...opts, uuid: () => "r-1" }));
    expect(result.current.available).toBe(true);
    await act(async () => { await result.current.resume(""); });
    expect(result.current.available).toBe(false); // the prop never changed
    expect(result.current.status).toContain("Opening this session");
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(getLocalSession).toHaveBeenCalledWith("s-wait");
    expect(result.current.status).toBe("Not resumed: its folder is no longer on Alice’s Mac.");
    expect(result.current.available).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(getLocalSession).toHaveBeenCalledTimes(1); // no longer pending: polling stopped
  });

  it("keeps asking while a resume waits, and shows a refusal that arrives minutes later", async () => {
    const pending = ended({ sessionId: "s-late", resume: { state: "pending", byUserId: "bob", requestedAt: new Date().toISOString() } });
    const refused = ended({ sessionId: "s-late", resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "SHARING_OFF" } });
    let wake = false;
    const getLocalSession = vi.fn(async () => (wake ? refused : pending));
    const client = { submitLocalSessionCommand: vi.fn(), resumeLocalSession: vi.fn(async () => pending), getLocalSession };
    vi.useFakeTimers();
    try {
      const { result, unmount } = renderHook(() => useSessionResume(client, pending, { ...opts, uuid: () => "r-1" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(119_000); });
      const asked = getLocalSession.mock.calls.length;
      expect(asked).toBeGreaterThan(15); // past the first 15 fast checks
      expect(result.current.status).toContain("Opening this session");
      wake = true; // the Mac wakes after 2 minutes and refuses
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
      expect(result.current.status).toBe("Not resumed: session sharing is off on Alice’s Mac.");
      const settled = getLocalSession.mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
      expect(getLocalSession.mock.calls.length).toBe(settled); // refused: polling stopped
      unmount();
    } finally { vi.useRealTimers(); }
  });
});
