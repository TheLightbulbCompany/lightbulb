/**
 * The amnesia bug (prod, 2026-08-09): a returning free-tier owner's General
 * session — 127 stored messages — rendered the new-chat bootstrap, because
 * the empty-read ladder (4.75s of patience) raced a container wake measured
 * at 5m30s and concluded "empty" 4½ minutes before the session store came up.
 *
 * The fix gates the conclusion on reachability: an empty read from an
 * UNREACHABLE container (health probe rejects) says nothing, so the hook
 * keeps the loading skeleton and re-reads on a slow cadence until the
 * container answers. Only a reachable container's empty read — after grace
 * re-reads for store-lag — may conclude bootstrap.
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

// Container simulator: while down, chat.history returns empty-but-200 and
// health REJECTS (pool can't reach the box). Once up, health resolves and
// history returns the stored transcript.
let containerUp = false;
const STORED = {
  messages: [
    { role: "user", content: [{ type: "text", text: "onboarding brief" }], timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "kickoff done yesterday" }], timestamp: 2 },
  ],
};

function armContainerMock(): void {
  sendReq.mockImplementation((method: string) => {
    if (method === "health") {
      return containerUp ? Promise.resolve({ ok: true }) : Promise.reject(new Error("gateway unreachable"));
    }
    if (method === "chat.history") {
      return Promise.resolve(containerUp ? STORED : { messages: [] });
    }
    return Promise.resolve({});
  });
}

describe("useAgentChat — empty reads from an unreachable container never conclude bootstrap", () => {
  beforeEach(() => {
    chatHandlers = [];
    containerUp = false;
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset();
    armContainerMock();
    vi.useFakeTimers();
    vi.resetModules();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("keeps the skeleton through a multi-minute wake and recovers the transcript when the container comes up", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // Quick ladder runs and exhausts against the waking container.
    await advance(0);
    await advance(500);
    await advance(1250);
    await advance(3000);
    expect(historyCalls()).toBe(4);
    // Probe rejects (unreachable) — the OLD behaviour concluded bootstrap
    // here. Now: no conclusion, skeleton stays, slow cadence armed.
    await advance(0);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);

    // Three minutes of wake: reads keep ticking every 5s, still no bootstrap.
    await advance(180_000);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);
    const readsDuringWake = historyCalls();
    expect(readsDuringWake).toBeGreaterThan(4);

    // Container finishes waking. The next slow read still sees the store
    // mid-load (empty) but the probe now RESOLVES -> grace re-read recovers
    // the full transcript.
    containerUp = true;
    await advance(10_000);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(false);
    expect(result.current.chatMessages.length).toBeGreaterThan(0);
  });

  it("gives up honestly at the wake ceiling instead of spinning forever", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await advance(0);
    await advance(500);
    await advance(1250);
    await advance(3000);
    // 120 slow attempts x 5s ≈ 10 minutes, plus probe microtasks.
    await advance(700_000);
    expect(result.current.needsBootstrap).toBe(true);
    expect(result.current.isLoadingHistory).toBe(false);
    const settled = historyCalls();
    await advance(60_000);
    expect(historyCalls()).toBe(settled);
  });
});
