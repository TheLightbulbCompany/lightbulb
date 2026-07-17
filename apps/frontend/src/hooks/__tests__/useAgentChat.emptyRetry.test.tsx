/**
 * Bounded-retry guard for the chat.history read (useAgentChat).
 *
 * On a full page refresh the module message cache is empty. The history fetch
 * then races the container: on the free tier the box scales to zero when idle,
 * so a post-refresh read can hit a not-ready / waking container and come back
 * empty-but-200 — which is NOT authoritatively empty. Treating it as empty
 * blanks the screen and offers the "start a conversation" bootstrap for a
 * conversation that actually exists (it "comes back" on the next message once
 * the container is up). The hook must instead retry a bounded number of times
 * with a short backoff before flagging bootstrap, while:
 *   - NEVER stomping a cached transcript (an empty read with a cache is kept),
 *   - still eventually offering bootstrap for a genuinely empty session,
 *   - recovering the transcript the moment a retry returns it.
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

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
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

/** Flush pending microtasks + timers up to `ms`, wrapped in act(). */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useAgentChat — empty chat.history read is retried, not treated as authoritative", () => {
  beforeEach(() => {
    chatHandlers = [];
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

  it("does NOT immediately flag bootstrap on an empty-no-cache read — it retries, and only bootstraps after retries exhaust", async () => {
    // Every history read is empty: a genuinely (or persistently-not-ready)
    // empty session.
    sendReq.mockImplementation(async () => ({ messages: [] }));

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // Initial read resolves empty. It must NOT bootstrap yet — it retries while
    // showing the loading skeleton, so the screen never flashes the bootstrap
    // suggestion for what might be a waking container.
    await advance(0);
    expect(historyCalls()).toBe(1);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);

    // Backoff schedule is [500, 1250, 3000]. Each elapsed delay fires one more
    // read; bootstrap stays false until the last retry also comes back empty.
    await advance(500);
    expect(historyCalls()).toBe(2);
    expect(result.current.needsBootstrap).toBe(false);

    await advance(1250);
    expect(historyCalls()).toBe(3);
    expect(result.current.needsBootstrap).toBe(false);

    await advance(3000);
    // Retries exhausted (4 reads total) — NOW it is authoritatively empty.
    expect(historyCalls()).toBe(4);
    expect(result.current.needsBootstrap).toBe(true);
    expect(result.current.isLoadingHistory).toBe(false);

    // And it does not keep retrying forever.
    await advance(10000);
    expect(historyCalls()).toBe(4);
  });

  it("recovers the transcript when a retry returns it (container finished waking) — no bootstrap", async () => {
    let reads = 0;
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // Empty on the first two reads (container still waking), then the store
        // is up and the real transcript loads.
        return reads >= 3 ? history("recovered answer") : { messages: [] };
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await advance(0); // read 1 (empty) → schedule retry
    expect(result.current.needsBootstrap).toBe(false);
    await advance(500); // read 2 (empty) → schedule retry
    expect(result.current.needsBootstrap).toBe(false);
    await advance(1250); // read 3 → transcript arrives

    expect(historyCalls()).toBe(3);
    expect(JSON.stringify(result.current.renderItems)).toContain("recovered answer");
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(false);

    // The loop stopped once the transcript arrived — no further reads.
    await advance(10000);
    expect(historyCalls()).toBe(3);
  });

  it("never stomps a cached transcript: an empty read with a cache keeps it, does not retry, does not bootstrap", async () => {
    let reads = 0;
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // First mount seeds the cache with a real transcript; the remount sees
        // a transient empty read.
        return reads >= 2 ? { messages: [] } : history("cached answer");
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const first = renderHook(() => useAgentChat("agent-X", "user-1"));
    await advance(0);
    expect(JSON.stringify(first.result.current.renderItems)).toContain("cached answer");
    first.unmount();

    // Remount: cached transcript paints instantly; the empty revalidation must
    // not clobber it, must not retry (hasCache short-circuits), must not bootstrap.
    const second = renderHook(() => useAgentChat("agent-X", "user-1"));
    expect(JSON.stringify(second.result.current.renderItems)).toContain("cached answer");
    await advance(0);
    expect(JSON.stringify(second.result.current.renderItems)).toContain("cached answer");
    expect(second.result.current.needsBootstrap).toBe(false);

    // Exactly 2 reads (initial seed + remount revalidation) — no retry storm.
    await advance(10000);
    expect(historyCalls()).toBe(2);
  });
});
