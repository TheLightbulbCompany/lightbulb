// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt } from "./types";
import type { RoomResourceSource } from "./useRoomResources";
import { canSendToSession, sessionLabel } from "./localSessionLabels";
import { AUTO_CHECK_INTERVAL_MS, useSessionCommand } from "./useSessionCommand";

const session: LocalSession = {
  sessionId: "session-123456", deviceId: "mac", humanUserId: "alice", provider: "claude",
  providerVersion: "2.1.224", generation: "g1", status: "idle", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  controlUnavailableReason: null, latestEventSeq: 10, title: "Fix relay",
};
const receipt = (request: LocalSessionCommand, state: LocalSessionCommandReceipt["state"] = "applied"): LocalSessionCommandReceipt => ({
  ...request, text: request.text ?? null, expectedTurnId: request.expectedTurnId ?? null,
  sessionId: session.sessionId, roomId: "room", actorUserId: "bob", commandSeq: 1, state,
  createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
});
// The auto-check budget the hook derives from THIS fixture's own deliverBy/createdAt,
// not a hardcoded 15 — matches useSessionCommand's `Math.ceil((deliverBy - createdAt) / (interval/1000))`.
const probeReceipt = receipt({ requestId: "probe", expectedGeneration: "g1", kind: "send", text: "" });
const AUTO_CHECK_LIMIT = Math.max(1, Math.ceil((probeReceipt.deliverBy - probeReceipt.createdAt) / (AUTO_CHECK_INTERVAL_MS / 1000)));
const submit = vi.fn();
const get = vi.fn();
const client = { listResources: vi.fn(), listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: submit, getLocalSessionCommand: get } as unknown as RoomResourceSource["client"];
const onAccessLost = vi.fn();
const onSent = vi.fn();
// A counter (not a fixed id) so a test can prove retry() reuses the id minted
// for the original send instead of asking uuid() for a fresh one.
let uuidCounter = 0;
const uuid = () => `req-${++uuidCounter}`;
const hook = (s: LocalSession = session) => renderHook(() => useSessionCommand(client, "room", s, { uuid, onAccessLost, onSent }));

afterEach(() => { cleanup(); vi.useRealTimers(); submit.mockReset(); get.mockReset(); onAccessLost.mockClear(); onSent.mockClear(); uuidCounter = 0; });

describe("labels", () => {
  it("labels a session by provider and title, and gates sending on verified control", () => {
    expect(sessionLabel(session)).toBe("Claude · Fix relay");
    expect(sessionLabel({ ...session, provider: "codex", title: null })).toBe("Codex · session-");
    expect(canSendToSession(session)).toBe(true);
    expect(canSendToSession({ ...session, status: "offline" })).toBe(false);
    expect(canSendToSession({ ...session, controlUnavailableReason: "history-gap" })).toBe(false);
    expect(canSendToSession({ ...session, status: "busy", capabilities: { ...session.capabilities, sendMode: "immediate" }, activeTurnId: null })).toBe(false);
  });
});

describe("useSessionCommand", () => {
  it("sends a trimmed follow-up and reports the receipt", async () => {
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = hook();
    await act(async () => { result.current.send("  hello  "); });
    expect(submit).toHaveBeenCalledWith("room", session.sessionId, { requestId: "req-1", expectedGeneration: "g1", kind: "send", text: "hello" });
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.receiptText).toBe("Admitted to the native session — not completed work.");
    expect(result.current.locked).toBe(false);
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("carries the active turn when input is immediate and the session is busy", async () => {
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = hook({ ...session, status: "busy", activeTurnId: "turn-9", capabilities: { ...session.capabilities, sendMode: "immediate" } });
    await act(async () => { result.current.send("go"); });
    expect(submit.mock.calls[0][2]).toMatchObject({ expectedTurnId: "turn-9" });
  });

  it("marks a 409 rejected with the refresh copy, and a 5xx uncertain with a retry", async () => {
    submit.mockRejectedValueOnce(new ApiError(409, null));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt).toMatchObject({ state: "rejected", error: "Session changed or is offline. Refresh before sending again." });
    submit.mockRejectedValueOnce(new ApiError(503, null));
    await act(async () => { result.current.send("again"); });
    expect(submit).toHaveBeenLastCalledWith("room", session.sessionId, expect.objectContaining({ requestId: "req-2", text: "again" }));
    expect(result.current.attempt).toMatchObject({ state: "uncertain", error: "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    expect(result.current.locked).toBe(true);
    expect(result.current.canCheck).toBe(true);
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    await act(async () => { result.current.retry(); });
    // The retry re-sends the SAME request (req-2), it never mints a fresh id (req-3).
    expect(submit).toHaveBeenLastCalledWith("room", session.sessionId, expect.objectContaining({ requestId: "req-2", text: "again" }));
    expect(result.current.attempt?.state).toBe("received");
  });

  it("check delivery resolves an uncertain send and clears the draft", async () => {
    submit.mockRejectedValueOnce(new TypeError("network"));
    get.mockImplementation(async () => receipt({ requestId: "req-1", expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt?.state).toBe("uncertain");
    expect(onSent).not.toHaveBeenCalled();
    await act(async () => { await result.current.checkDelivery(); });
    expect(get).toHaveBeenCalledWith("room", session.sessionId, "req-1");
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.receiptText).toBe("Dispatched; native admission is not yet confirmed.");
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("refuses a follow-up over 8 KiB without calling the server", async () => {
    const { result } = hook();
    await act(async () => { result.current.send("x".repeat(8200)); });
    expect(submit).not.toHaveBeenCalled();
    expect(result.current.attempt).toMatchObject({ state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
  });

  it("does nothing when the session cannot take input, and revokes on 403", async () => {
    const { result } = hook({ ...session, capabilities: { ...session.capabilities, sendMode: "none" } });
    expect(result.current.canSend).toBe(false);
    await act(async () => { result.current.send("hello"); });
    expect(submit).not.toHaveBeenCalled();
    submit.mockRejectedValueOnce(new ApiError(403, null));
    const open = hook();
    await act(async () => { open.result.current.send("hello"); });
    expect(onAccessLost).toHaveBeenCalledTimes(1);
  });

  it("auto-settles a pending receipt on the first check: no canCheck at any point, checking never true", async () => {
    vi.useFakeTimers();
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request, "pending"));
    get.mockImplementation(async (_r: string, _s: string, requestId: string) => receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, "applied"));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.canCheck).toBe(false);
    expect(result.current.checking).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith("room", session.sessionId, "req-1");
    expect(result.current.receiptText).toBe("Admitted to the native session — not completed work.");
    expect(result.current.canCheck).toBe(false);
    expect(result.current.checking).toBe(false);

    // Settled: no further auto-checks are scheduled.
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it(`keeps canCheck false while a receipt stays dispatched, flips true only after the ${AUTO_CHECK_LIMIT}-check budget, checking exactly ${AUTO_CHECK_LIMIT} times`, async () => {
    vi.useFakeTimers();
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request, "dispatched"));
    get.mockImplementation(async (_r: string, _s: string, requestId: string) => receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.canCheck).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_LIMIT * AUTO_CHECK_INTERVAL_MS - 1); }); // just under the window
    expect(result.current.canCheck).toBe(false);
    expect(get.mock.calls.length).toBeLessThan(AUTO_CHECK_LIMIT);

    await act(async () => { await vi.advanceTimersByTimeAsync(1); }); // the last check lands
    expect(get).toHaveBeenCalledTimes(AUTO_CHECK_LIMIT);
    expect(result.current.canCheck).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); }); // exhausted: no further calls
    expect(get).toHaveBeenCalledTimes(AUTO_CHECK_LIMIT);
  });

  it("stops auto-checking once the component unmounts", async () => {
    vi.useFakeTimers();
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request, "dispatched"));
    get.mockImplementation(async (_r: string, _s: string, requestId: string) => receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    const { result, unmount } = hook();
    await act(async () => { result.current.send("hello"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(get).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("prefixes an agent-issued receipt with the agent", async () => {
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand): Promise<LocalSessionCommandReceipt> => ({
      ...request, text: request.text ?? null, expectedTurnId: null, sessionId: session.sessionId, roomId: "room",
      commandSeq: 1, state: "pending", createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
      actor: { kind: "agent", id: "friday" }, invocationExpiresAt: 99,
    }));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.receiptText).toBe("Agent friday: Pending delivery to the desktop.");
  });
});
