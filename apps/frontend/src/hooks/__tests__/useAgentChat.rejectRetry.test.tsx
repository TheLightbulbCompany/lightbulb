/**
 * A REJECTED chat.history read must not blank the transcript (useAgentChat).
 *
 * The empty-retry ladder only covers reads that RESOLVE with an empty array.
 * A read whose response is LOST — the gateway socket parked/superseded/closed
 * mid-flight, the 30s RPC timeout, or a transient backend `ok:false`
 * ("container is starting up") — REJECTS instead. The old `.catch` un-latched
 * and flipped to done+empty, painting the "start a conversation" screen over a
 * real transcript until a full reload (prod, 2026-08-09: a returning owner's
 * General session went blank on refresh while the container was reachable and
 * held the transcript).
 *
 * The fix routes a reject into the SAME wall-clock-bounded wake ladder the
 * unreachable-empty branch uses: keep the loading skeleton and re-read on the
 * slow cadence (`sendReq` reconnects itself, so the retry recovers the
 * park/supersede cases that never toggle isConnected), never setChatMessages([])
 * and never bootstrap, bounded by the wake deadline so it cannot spin forever.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn();
const sendChat = vi.fn();
const send = vi.fn();
let mockConnected = true;

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: mockConnected,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: () => () => {},
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

const GENERAL_KEY = "agent:agent-X:user-1";
// Mirrors HISTORY_WAKE_RETRY_MS in useAgentChat.ts (5s wake cadence).
const WAKE_RETRY_MS = 5_000;
// Mirrors HISTORY_WAKE_DEADLINE_MS in useAgentChat.ts (10min wall-clock cap).
const WAKE_DEADLINE_MS = 10 * 60_000;

function history(text: string): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      { role: "user", content: [{ type: "text", text: "q" }], timestamp: 1 },
      { role: "assistant", content: [{ type: "text", text }], timestamp: 2 },
    ],
  };
}

function historyCalls(): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === GENERAL_KEY,
  ).length;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useAgentChat — a rejected chat.history is retried, never blanked to bootstrap", () => {
  beforeEach(() => {
    chatHandlers = [];
    mockConnected = true;
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset();
    vi.useFakeTimers();
    vi.resetModules();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("keeps the loading skeleton on a reject (NOT the empty/bootstrap screen), then recovers the transcript on the wake retry", async () => {
    let reads = 0;
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // First read's response is LOST (backend transient / socket drop);
        // the wake retry lands on a ready container and returns the transcript.
        if (reads === 1) {
          return Promise.reject(new Error("Container is starting up. Try again in a moment."));
        }
        return Promise.resolve(history("recovered answer"));
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // Read 1 REJECTS. With the old code this un-latched + flipped to done+empty
    // (bootstrap screen). Fixed: skeleton stays, no bootstrap, no false empty.
    await advance(0);
    expect(historyCalls()).toBe(1);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);
    expect(JSON.stringify(result.current.renderItems)).not.toContain("recovered answer");

    // The wake retry fires after the 5s cadence and returns the real transcript.
    await advance(WAKE_RETRY_MS);
    expect(historyCalls()).toBe(2);
    expect(JSON.stringify(result.current.renderItems)).toContain("recovered answer");
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(false);

    // The loop stopped the instant a read resolved — no runaway retries.
    await advance(WAKE_DEADLINE_MS);
    expect(historyCalls()).toBe(2);
  });

  it("a persistently-rejecting read is wall-clock bounded — it keeps retrying, then stops instead of spinning forever", async () => {
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        return Promise.reject(new Error("Container is starting up"));
      }
      return Promise.reject(new Error("unreachable"));
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // First reject: skeleton stays, no bootstrap.
    await advance(0);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);

    // It retries on the 5s cadence while the container never answers.
    await advance(WAKE_RETRY_MS);
    await advance(WAKE_RETRY_MS);
    const midCount = historyCalls();
    expect(midCount).toBeGreaterThanOrEqual(3);

    // The wall-clock deadline caps the retry chain. Advance well past it: it
    // must retry across the window, then STOP (no infinite loop) and drop the
    // skeleton. `Date.now()`, not attempt-count, is what bounds this (a hung
    // RPC could make N attempts take far longer than the window).
    await advance(WAKE_DEADLINE_MS);
    const afterDeadline = historyCalls();
    expect(afterDeadline).toBeGreaterThan(midCount);

    await advance(WAKE_DEADLINE_MS);
    expect(historyCalls()).toBe(afterDeadline); // stopped
    expect(result.current.isLoadingHistory).toBe(false);
  });

  it("does not stomp a cached transcript when a reject lands on a remount — cache keeps rendering during the silent retry", async () => {
    let reads = 0;
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // First mount seeds the cache; the remount's revalidation REJECTS.
        if (reads === 1) return Promise.resolve(history("cached answer"));
        return Promise.reject(new Error("Container is starting up"));
      }
      return Promise.reject(new Error("unreachable"));
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const first = renderHook(() => useAgentChat("agent-X", "user-1"));
    await advance(0);
    expect(JSON.stringify(first.result.current.renderItems)).toContain("cached answer");
    first.unmount();

    // Remount: cached transcript paints instantly; the rejecting revalidation
    // must never blank it.
    const second = renderHook(() => useAgentChat("agent-X", "user-1"));
    expect(JSON.stringify(second.result.current.renderItems)).toContain("cached answer");
    await advance(0);
    await advance(WAKE_RETRY_MS);
    expect(JSON.stringify(second.result.current.renderItems)).toContain("cached answer");
    expect(second.result.current.needsBootstrap).toBe(false);
  });
});
