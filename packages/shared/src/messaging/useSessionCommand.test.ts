// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt } from "./types";
import type { SessionSource } from "./useLocalSessionHistory";
import type { SessionControlClient } from "./sessionControls";
import { canSendToSession, sessionLabel } from "./localSessionLabels";
import { AUTO_CHECK_INTERVAL_MS, pendingPrompt, receiptChip, useSessionCommand, useSessionOutbox, useSessionSendReceipt, type SessionCommandAttempt } from "./useSessionCommand";

const session: LocalSession = {
  sessionId: "session-123456", deviceId: "mac", humanUserId: "alice", provider: "claude",
  providerVersion: "2.1.224", generation: "g1", status: "idle", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  controlUnavailableReason: null, latestEventSeq: 10, title: "Fix relay",
};
const receipt = (request: LocalSessionCommand, state: LocalSessionCommandReceipt["state"] = "applied"): LocalSessionCommandReceipt => ({
  ...request, text: request.text ?? null, expectedTurnId: request.expectedTurnId ?? null,
  sessionId: session.sessionId, roomId: "room", actorUserId: "bob", commandSeq: 1, state,
  createdAt: 1, deliverBy: 31, held: false, providerMessageId: null, replyPosted: false,
});
// The auto-check budget the hook derives from THIS fixture's own deliverBy/createdAt,
// not a hardcoded 15 — matches useSessionCommand's `Math.ceil((deliverBy - createdAt) / (interval/1000))`.
const probeReceipt = receipt({ requestId: "probe", expectedGeneration: "g1", kind: "send", text: "" });
const AUTO_CHECK_LIMIT = Math.max(1, Math.ceil((probeReceipt.deliverBy - probeReceipt.createdAt) / (AUTO_CHECK_INTERVAL_MS / 1000)));
const submit = vi.fn();
const get = vi.fn();
const client = { listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: submit, getLocalSessionCommand: get } as unknown as SessionSource["client"];
const onAccessLost = vi.fn();
const onSent = vi.fn();
// A counter (not a fixed id) so a test can prove retry() reuses the id minted
// for the original send instead of asking uuid() for a fresh one.
let uuidCounter = 0;
const uuid = () => `req-${++uuidCounter}`;
const hook = (s: LocalSession = session) => renderHook(() => useSessionCommand(client, s, { uuid, onAccessLost, onSent }));

afterEach(() => { cleanup(); vi.useRealTimers(); submit.mockReset(); get.mockReset(); onAccessLost.mockClear(); onSent.mockClear(); uuidCounter = 0; });

describe("useSessionOutbox", () => {
  it("ignores an older refresh that resolves after a cancelled receipt", async () => {
    const request = { requestId: "r1", expectedGeneration: "g1", kind: "send" as const, text: "continue" };
    const pending = receipt(request, "pending");
    const cancelled = { ...pending, state: "rejected" as const, cancelledAt: 42 };
    let resolveOld!: (page: { commands: LocalSessionCommandReceipt[] }) => void;
    const old = new Promise<{ commands: LocalSessionCommandReceipt[] }>(resolve => { resolveOld = resolve; });
    const list = vi.fn().mockReturnValueOnce(old).mockResolvedValue({ commands: [cancelled] });
    const outboxClient = { listLocalSessionOutbox: list } as unknown as SessionControlClient;
    const { result } = renderHook(() => useSessionOutbox(outboxClient, session.sessionId, 0, false));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.commands[0].cancelledAt).toBe(42);
    await act(async () => { resolveOld({ commands: [pending] }); await old; });
    expect(result.current.commands[0].cancelledAt).toBe(42);
  });

  it("rechecks an offline outbox even when no commands were visible", async () => {
    vi.useFakeTimers();
    const list = vi.fn().mockResolvedValue({ commands: [] });
    const outboxClient = { listLocalSessionOutbox: list } as unknown as SessionControlClient;
    renderHook(() => useSessionOutbox(outboxClient, session.sessionId, 0, false));
    await act(async () => {});
    expect(list).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("keeps checking an unresolved online receipt beyond the first three polls", async () => {
    vi.useFakeTimers();
    const now = Math.floor(Date.now() / 1000);
    const waiting = { ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "continue" }, "pending"),
      createdAt: now, deliverBy: now + 86400 };
    const list = vi.fn().mockResolvedValue({ commands: [waiting] });
    const outboxClient = { listLocalSessionOutbox: list } as unknown as SessionControlClient;
    renderHook(() => useSessionOutbox(outboxClient, session.sessionId, 0, true));
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(list).toHaveBeenCalledTimes(5);
  });
});

describe("useSessionSendReceipt", () => {
  it("shows a stored rejection without fetching", () => {
    const { result } = renderHook(() => useSessionSendReceipt(get, { sessionId: "s1", error: "LOCAL_SESSION_OFFLINE" }));
    expect(result.current.chip).toEqual({ label: "Not delivered", tone: "error", detail: "LOCAL_SESSION_OFFLINE" });
    expect(get).not.toHaveBeenCalled();
  });

  it("checks a command and shows its applied receipt", async () => {
    get.mockResolvedValue(receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }));
    const { result } = renderHook(() => useSessionSendReceipt(get, { sessionId: "s1", requestId: "r1" }));
    expect(result.current.chip?.label).toBe("Sending");
    await waitFor(() => expect(result.current.chip?.label).toBe("Delivered"));
    expect(get).toHaveBeenCalledWith("s1", "r1");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("keeps checking a held room send until its exact native reply is posted", async () => {
    vi.useFakeTimers();
    get.mockResolvedValueOnce({ ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }), held: true })
      .mockResolvedValueOnce({ ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }), held: true, replyPosted: true });
    const { result } = renderHook(() => useSessionSendReceipt(get, { sessionId: "s1", requestId: "r1" }));
    await act(async () => {});
    expect(result.current.chip?.label).toBe("Queued on your Mac");
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(result.current.chip?.label).toBe("Delivered");
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("stops polling after the bounded held-reply window", async () => {
    vi.useFakeTimers();
    get.mockResolvedValue({ ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }), held: true });
    renderHook(() => useSessionSendReceipt(get, { sessionId: "s1", requestId: "r1" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(121_000); });
    const calls = get.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); });
    expect(get).toHaveBeenCalledTimes(calls);
  });

  it("refetches after room replies change and trusts only replyPosted", async () => {
    vi.useFakeTimers();
    get.mockResolvedValueOnce({ ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }), held: true })
      .mockResolvedValueOnce({ ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }), held: true })
      .mockResolvedValueOnce({ ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }), held: true, replyPosted: true });
    const { result, rerender } = renderHook(({ replies }) => useSessionSendReceipt(get, { sessionId: "s1", requestId: "r1" }, replies), {
      initialProps: { replies: 0 },
    });
    await act(async () => { await Promise.resolve(); });
    expect(result.current.chip?.label).toBe("Queued on your Mac");
    rerender({ replies: 1 });
    await act(async () => { await Promise.resolve(); });
    expect(result.current.chip?.label).toBe("Queued on your Mac");
    expect(get).toHaveBeenCalledTimes(2);
    rerender({ replies: 2 });
    await act(async () => { await Promise.resolve(); });
    expect(result.current.chip?.label).toBe("Delivered");
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("restarts the bounded poll window after a late room reply invalidation", async () => {
    vi.useFakeTimers();
    get.mockImplementation(async () => ({
      ...receipt({ requestId: "r1", expectedGeneration: "g1", kind: "send", text: "hi" }),
      held: true,
      replyPosted: get.mock.calls.length >= 62,
    }));
    const { result, rerender } = renderHook(({ replies }) => useSessionSendReceipt(get, { sessionId: "s1", requestId: "r1" }, replies), {
      initialProps: { replies: 0 },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(121_000); });
    expect(get).toHaveBeenCalledTimes(60);
    expect(result.current.chip?.label).toBe("Queued on your Mac");

    rerender({ replies: 1 });
    await act(async () => { await Promise.resolve(); });
    expect(get).toHaveBeenCalledTimes(61);
    expect(result.current.chip?.label).toBe("Queued on your Mac");
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(get).toHaveBeenCalledTimes(62);
    expect(result.current.chip?.label).toBe("Delivered");
  });

  it("renders nothing without a command reader", () => {
    const { result } = renderHook(() => useSessionSendReceipt(undefined, { sessionId: "s1", requestId: "r1" }));
    expect(result.current.chip).toBeNull();
  });
});

describe("labels", () => {
  it("labels a session by provider and title, and gates sending on verified control", () => {
    expect(sessionLabel(session)).toBe("Claude · Fix relay");
    expect(sessionLabel({ ...session, provider: "codex", title: null })).toBe("Codex · session-");
    expect(canSendToSession(session)).toBe(true);
    expect(canSendToSession({ ...session, status: "offline" })).toBe(false);
    expect(canSendToSession({ ...session, status: "offline" }, true)).toBe(true);
    expect(canSendToSession({ ...session, status: "offline", capabilities: { ...session.capabilities, sendMode: "immediate" } })).toBe(false);
    expect(canSendToSession({ ...session, controlUnavailableReason: "history-gap" })).toBe(false);
    expect(canSendToSession({ ...session, status: "busy", capabilities: { ...session.capabilities, sendMode: "immediate" }, activeTurnId: null })).toBe(false);
  });
});

describe("useSessionCommand", () => {
  it("keeps Send queued and binds explicit Steer and Stop to the active Codex turn", async () => {
    const live = { ...session, provider: "codex" as const, status: "busy" as const, activeTurnId: "turn-1",
      capabilities: { sendMode: "queue" as const, steer: true, interrupt: true } };
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = renderHook(() => useSessionCommand(client, live, { uuid, onAccessLost, onSent, allowInterrupt: true }));
    expect(result.current.canSteer).toBe(true);
    expect(result.current.canInterrupt).toBe(true);
    await act(async () => { result.current.send("next"); });
    expect(submit).toHaveBeenLastCalledWith(session.sessionId, expect.objectContaining({ kind: "send", text: "next" }));
    expect(submit.mock.calls[0][1]).not.toHaveProperty("expectedTurnId");
    await act(async () => { result.current.steer("now"); });
    expect(submit).toHaveBeenLastCalledWith(session.sessionId, expect.objectContaining({ kind: "send", text: "now", expectedTurnId: "turn-1" }));
    await act(async () => { result.current.interrupt(); });
    expect(submit).toHaveBeenLastCalledWith(session.sessionId, expect.objectContaining({ kind: "interrupt", expectedTurnId: "turn-1" }));
    expect(result.current.chip?.label).toBe("Stop requested");
    expect(onSent).toHaveBeenCalledTimes(2); // Stop never clears an unsent draft.
  });

  it("queues an offline follow-up without claiming it reached the Mac", async () => {
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => ({
      ...receipt(request, "pending"), createdAt: Math.floor(Date.now() / 1000),
      deliverBy: Math.floor(Date.now() / 1000) + 86400,
    }));
    const { result } = renderHook(() => useSessionCommand(client, { ...session, status: "offline" },
      { uuid, onAccessLost, onSent, allowOffline: true }));
    await act(async () => { result.current.send("continue"); });
    expect(submit).toHaveBeenCalledWith(session.sessionId, expect.objectContaining({ kind: "send", text: "continue" }));
    expect(result.current.chip).toEqual({
      label: "Queued for session", tone: "pending", detail: "The Mac will recheck this session before delivery. Expires after 24 hours.",
    });
    await act(async () => { result.current.syncReceipt({ ...result.current.receipt!, state: "rejected", cancelledAt: 42 }); });
    expect(result.current.chip?.label).toBe("Cancelled");
  });

  it("sends a trimmed follow-up and reports the receipt", async () => {
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = hook();
    await act(async () => { result.current.send("  hello  "); });
    expect(submit).toHaveBeenCalledWith(session.sessionId, { requestId: "req-1", expectedGeneration: "g1", kind: "send", text: "hello" });
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.chip?.label).toBe("Delivered");
    expect(result.current.locked).toBe(false);
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("carries the active turn when input is immediate and the session is busy", async () => {
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = hook({ ...session, status: "busy", activeTurnId: "turn-9", capabilities: { ...session.capabilities, sendMode: "immediate" } });
    await act(async () => { result.current.send("go"); });
    expect(submit.mock.calls[0][1]).toMatchObject({ expectedTurnId: "turn-9" });
  });

  it("marks a 409 rejected with the refresh copy, and a 5xx uncertain with a retry", async () => {
    submit.mockRejectedValueOnce(new ApiError(409, null));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt).toMatchObject({ state: "rejected", error: "Session changed or is offline. Refresh before sending again." });
    submit.mockRejectedValueOnce(new ApiError(503, null));
    await act(async () => { result.current.send("again"); });
    expect(submit).toHaveBeenLastCalledWith(session.sessionId, expect.objectContaining({ requestId: "req-2", text: "again" }));
    expect(result.current.attempt).toMatchObject({ state: "uncertain", error: "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    expect(result.current.locked).toBe(true);
    expect(result.current.canCheck).toBe(true);
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request));
    await act(async () => { result.current.retry(); });
    // The retry re-sends the SAME request (req-2), it never mints a fresh id (req-3).
    expect(submit).toHaveBeenLastCalledWith(session.sessionId, expect.objectContaining({ requestId: "req-2", text: "again" }));
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
    expect(get).toHaveBeenCalledWith(session.sessionId, "req-1");
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.chip).toEqual({ label: "Sending", tone: "pending", detail: "Waiting for the session to accept it." });
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
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request, "pending"));
    get.mockImplementation(async (_s: string, requestId: string) => receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, "applied"));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.canCheck).toBe(false);
    expect(result.current.checking).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith(session.sessionId, "req-1");
    expect(result.current.chip?.label).toBe("Delivered");
    expect(result.current.canCheck).toBe(false);
    expect(result.current.checking).toBe(false);

    // Settled: no further auto-checks are scheduled.
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it(`keeps canCheck false while a receipt stays dispatched, flips true only after the ${AUTO_CHECK_LIMIT}-check budget, checking exactly ${AUTO_CHECK_LIMIT} times`, async () => {
    vi.useFakeTimers();
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request, "dispatched"));
    get.mockImplementation(async (_s: string, requestId: string) => receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
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

  it("keeps checking through a transient unknown until the desktop reconciles it to Delivered", async () => {
    // Prod 2026-09-25: the first receipt raced the transcript write (unknown), the next
    // desktop heartbeat settled it applied, and the composer stayed locked on unknown.
    vi.useFakeTimers();
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request, "pending"));
    const next = (state: LocalSessionCommandReceipt["state"]) => async (_s: string, requestId: string) =>
      receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, state);
    get.mockImplementationOnce(next("unknown")).mockImplementationOnce(next("unknown")).mockImplementation(next("applied"));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(result.current.receipt?.state).toBe("unknown");
    expect(result.current.locked).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 2); });
    expect(result.current.chip?.label).toBe("Delivered");
    expect(result.current.locked).toBe(false);
    expect(result.current.canCheck).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); });
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("stops auto-checking once the component unmounts", async () => {
    vi.useFakeTimers();
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request, "dispatched"));
    get.mockImplementation(async (_s: string, requestId: string) => receipt({ requestId, expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    const { result, unmount } = hook();
    await act(async () => { result.current.send("hello"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS); });
    expect(get).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_CHECK_INTERVAL_MS * 5); });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("names the agent on an agent-issued receipt", async () => {
    submit.mockImplementation(async (_s: string, request: LocalSessionCommand): Promise<LocalSessionCommandReceipt> => ({
      ...request, text: request.text ?? null, expectedTurnId: null, sessionId: session.sessionId, roomId: "room",
      commandSeq: 1, state: "pending", createdAt: 1, deliverBy: 31, held: false, replyPosted: false, providerMessageId: null,
      actor: { kind: "agent", id: "friday" }, invocationExpiresAt: 99,
    }));
    const { result } = renderHook(() => useSessionCommand(client, session, { uuid, onAccessLost, onSent, agentName: (id) => (id === "friday" ? "Friday" : id) }));
    await act(async () => { result.current.send("hello"); });
    expect(result.current.chip?.label).toBe("Friday · Sending");
  });
});

describe("receiptChip / pendingPrompt", () => {
  const request: LocalSessionCommand = { requestId: "r", expectedGeneration: "g1", kind: "send", text: "hello" };
  it("maps every state onto one short chip", () => {
    expect(receiptChip({ request, state: "sending" })?.label).toBe("Sending");
    expect(receiptChip({ request, state: "uncertain" })).toMatchObject({ label: "Delivery unknown", tone: "warn" });
    expect(receiptChip({ request, state: "rejected", error: "too long" })).toEqual({ label: "Not delivered", tone: "error", detail: "too long" });
    expect(receiptChip({ request, state: "received", receipt: { ...receipt(request), held: true } })?.label).toBe("Queued on your Mac");
    expect(receiptChip({ request, state: "received", receipt: receipt(request, "unknown") })?.label).toBe("Delivery unknown");
    expect(receiptChip({ request, state: "received", receipt: receipt(request, "rejected") })?.label).toBe("Not delivered");
  });
  it("shows the pending row until the harness echoes the prompt after the baseline", () => {
    const attempt: SessionCommandAttempt = { request, state: "received", receipt: receipt(request) };
    const echo = (seq: number) => ({ seq, kind: "text", role: "user", text: "hello" }) as never;
    expect(pendingPrompt(attempt, [echo(3)], 5)).toBe("hello");
    expect(pendingPrompt(attempt, [echo(3), echo(6)], 5)).toBeNull();
    expect(pendingPrompt({ request, state: "rejected", error: "x" }, [], 0)).toBeNull();
  });
});
