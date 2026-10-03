// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import { canResumeSession, resumeStatus, useSessionResume } from "./sessionControls";
import type { LocalSession } from "./types";

const ended = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.288",
  generation: "g1", status: "ended", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, resumeAllowed: ["bob"], ...changes,
});
const nameOf = (id: string) => (id === "bob" ? "Bob" : "Someone");
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("canResumeSession", () => {
  it("is the owner's and the people who could send when it ended, once, on an ended session", () => {
    expect(canResumeSession(ended(), true)).toBe(true);
    expect(canResumeSession(ended(), false, "bob")).toBe(true);
    expect(canResumeSession(ended(), false, "carol")).toBe(false);
    expect(canResumeSession(ended({ status: "idle" }), true)).toBe(false);
    expect(canResumeSession(ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "" } }), true)).toBe(false);
    expect(canResumeSession(ended({ resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "FOLDER_MISSING" } }), false, "bob")).toBe(true);
  });
});

describe("resumeStatus", () => {
  it("says who asked and whose Mac it waits for, or why the Mac refused", () => {
    expect(resumeStatus(ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "" } }), false, "Alice", nameOf, "bob"))
      .toBe("You asked to resume this session. It starts on Alice’s Mac when that Mac is on, within 24 hours.");
    expect(resumeStatus(ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "" } }), true, "Alice", nameOf, "alice"))
      .toBe("Bob asked to resume this session. It starts on your Mac when that Mac is on, within 24 hours.");
    expect(resumeStatus(ended({ resume: { state: "failed", byUserId: "bob", requestedAt: "", reason: "ALREADY_RUNNING" } }), false, "Alice", nameOf, "bob"))
      .toBe("Not resumed: it is already running on Alice’s Mac.");
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
});
