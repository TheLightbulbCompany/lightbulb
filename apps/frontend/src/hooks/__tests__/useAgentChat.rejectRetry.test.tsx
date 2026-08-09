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
// NOTE: GatewayRpcError is imported DYNAMICALLY inside each test that needs it.
// beforeEach calls vi.resetModules(), and the hook is imported after that, so a
// top-level static import would be a DIFFERENT class instance than the hook's
// fresh one — `err instanceof GatewayRpcError` would silently be false. The
// dynamic import shares the hook's post-reset module registry.

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
        // First read's response is LOST to a transport drop — the socket
        // parked/superseded/closed mid-flight (a plain Error, not a
        // GatewayRpcError): the primary refresh-churn case. The wake retry
        // lands on a live socket + ready container and returns the transcript.
        if (reads === 1) {
          return Promise.reject(new Error("WebSocket closed"));
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

  it("a RETRYABLE GatewayRpcError (container starting up, retryable:true) keeps the skeleton and retries, then recovers", async () => {
    const { GatewayRpcError } = await import("@isol8/shared");
    let reads = 0;
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        if (reads === 1) {
          return Promise.reject(
            new GatewayRpcError({
              message: "Container is starting up. Try again in a moment.",
              retryable: true,
              retryAfterMs: 3000,
            }),
          );
        }
        return Promise.resolve(history("recovered answer"));
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await advance(0);
    expect(historyCalls()).toBe(1);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);

    await advance(WAKE_RETRY_MS);
    expect(historyCalls()).toBe(2);
    expect(JSON.stringify(result.current.renderItems)).toContain("recovered answer");
    expect(result.current.isLoadingHistory).toBe(false);
  });

  it("a NON-retryable GatewayRpcError (No container provisioned) terminates immediately — no retry, does not hold the loading shell", async () => {
    const { GatewayRpcError } = await import("@isol8/shared");
    let reads = 0;
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // Deterministic failure: it can never succeed on retry.
        return Promise.reject(new GatewayRpcError({ message: "No container provisioned." }));
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // One read, then TERMINAL: it must NOT retry across the whole wake window,
    // and the loading shell is released (input re-enabled) rather than spun for
    // 10min on a request that can never succeed.
    await advance(0);
    expect(historyCalls()).toBe(1);

    await advance(WAKE_DEADLINE_MS);
    await advance(WAKE_DEADLINE_MS);
    expect(historyCalls()).toBe(1);
    expect(reads).toBe(1);
    expect(result.current.isLoadingHistory).toBe(false);
  });

  it("a persistently-rejecting read is wall-clock bounded — it keeps retrying, then stops instead of spinning forever", async () => {
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        return Promise.reject(new Error("WebSocket closed")); // transport drop — retryable
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
        // First mount seeds the cache; the remount's revalidation REJECTS on a
        // transport drop.
        if (reads === 1) return Promise.resolve(history("cached answer"));
        return Promise.reject(new Error("WebSocket closed"));
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
